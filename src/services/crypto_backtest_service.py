# -*- coding: utf-8 -*-
"""BTC plan-level backtest orchestration service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
import logging
from typing import Any, Optional

import pandas as pd

from data_provider.crypto_fetcher import CryptoFetcher, is_crypto_code, normalize_crypto_symbol
from src.config import get_config
from src.core.crypto_backtest_engine import (
    CryptoBacktestEngine,
    CryptoPlan,
    CryptoPlanBacktestConfig,
)
from src.repositories.crypto_backtest_repo import CryptoBacktestRepository
from src.schemas.crypto_instrument import resolve_crypto_instrument
from src.storage import (
    AnalysisHistory,
    CryptoBacktestResult,
    CryptoBacktestSummary,
    DatabaseManager,
)
from src.utils.data_processing import parse_json_field
from src.utils.sniper_points import extract_directional_strategy_plans, parse_sniper_value
from src.utils.timeframe import analysis_timeframe_label, horizon_to_analysis_mode

logger = logging.getLogger(__name__)

BTC_PLAN_ENGINE_VERSION = "btc-plan-v5"


@dataclass(frozen=True)
class _Bar:
    timestamp: datetime
    high: float
    low: float
    close: float
    open: Optional[float] = None
    volume: Optional[float] = None
    volume_ratio: Optional[float] = None
    vwap: Optional[float] = None
    execution_open: Optional[float] = None
    execution_high: Optional[float] = None
    execution_low: Optional[float] = None
    execution_close: Optional[float] = None
    mark_open: Optional[float] = None
    mark_high: Optional[float] = None
    mark_low: Optional[float] = None
    mark_close: Optional[float] = None
    funding_rates: tuple[float, ...] = ()
    funding_complete: bool = False


@dataclass(frozen=True)
class _BarBatch:
    bars: list[_Bar]
    metadata: dict[str, Any]


class CryptoBacktestService:
    """Run and query BTC report plan backtests."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        config = get_config()
        self.db = db_manager or DatabaseManager.get_instance()
        self.repo = CryptoBacktestRepository(self.db)
        self.fetcher = CryptoFetcher(
            fetch_budget_seconds=float(getattr(config, "crypto_market_fetch_budget_seconds", 60.0)),
            fetch_max_pages=int(getattr(config, "crypto_market_fetch_max_pages", 200)),
            fetch_retry_count=int(getattr(config, "crypto_market_fetch_retry_count", 2)),
        )

    def run_backtest(
        self,
        *,
        code: Optional[str] = None,
        force: bool = False,
        min_age_hours: Optional[int] = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        config = get_config()
        engine_version = self._engine_version()
        if min_age_hours is None:
            min_age_hours = int(getattr(config, "crypto_backtest_min_age_hours", 24))

        candidates = self.repo.get_candidates(
            code=code,
            min_age_hours=int(min_age_hours),
            limit=int(limit),
            engine_version=engine_version,
            force=force,
        )

        processed = 0
        saved = 0
        completed = 0
        insufficient = 0
        skipped = 0
        errors = 0
        results: list[CryptoBacktestResult] = []

        bars_cache: dict[tuple[str, str, str, str, int], _BarBatch] = {}
        eval_config = self._eval_config(config, engine_version)
        existing_keys = {
            (int(row.analysis_history_id), str(row.plan_type))
            for row in self.repo.get_results_for_history_ids(
                analysis_history_ids=[int(row.id) for row in candidates if row.id is not None],
                engine_version=engine_version,
            )
            if row.eval_status not in {"pending", "insufficient_data"}
        }

        for analysis in candidates:
            processed += 1
            try:
                evaluated, counts = self._evaluate_analysis_record(
                    analysis=analysis,
                    engine_version=engine_version,
                    eval_config=eval_config,
                    bars_cache=bars_cache,
                    existing_keys=existing_keys,
                )
                if not evaluated:
                    skipped += 1
                    continue
                completed += counts["completed"]
                insufficient += counts["insufficient"]
                skipped += counts["skipped"]
                errors += counts["errors"]
                results.extend(evaluated)
            except Exception as exc:
                errors += 1
                logger.warning("BTC 回测失败: %s#%s: %s", analysis.code, analysis.id, exc)

        if results:
            saved = self.repo.save_results_batch(results, replace_existing=force)
            self._recompute_summaries(engine_version=engine_version)

        return {
            "processed": processed,
            "saved": saved,
            "completed": completed,
            "insufficient": insufficient,
            "skipped": skipped,
            "errors": errors,
        }

    def run_selected_backtests(
        self,
        *,
        analysis_history_ids: list[int],
        plan_types: Optional[list[str]] = None,
        force: bool = False,
    ) -> dict[str, Any]:
        ids = sorted({int(item) for item in analysis_history_ids if item is not None})
        if not ids:
            return {"processed": 0, "saved": 0, "completed": 0, "insufficient": 0, "skipped": 0, "errors": 0}

        engine_version = self._engine_version()
        records, _total = self.repo.get_history_records(ids=ids, offset=0, limit=len(ids))
        by_id = {int(record.id): record for record in records if record.id is not None}
        selected_plan_types = {str(item).strip() for item in (plan_types or []) if str(item).strip()}
        config = get_config()
        eval_config = self._eval_config(config, engine_version)
        existing_keys: set[tuple[int, str]] = set()
        if not force:
            existing_keys = {
                (int(row.analysis_history_id), str(row.plan_type))
                for row in self.repo.get_results_for_history_ids(
                    analysis_history_ids=ids,
                    engine_version=engine_version,
                )
                if row.eval_status not in {"pending", "insufficient_data"}
            }

        processed = 0
        completed = 0
        insufficient = 0
        skipped = 0
        errors = 0
        results: list[CryptoBacktestResult] = []
        bars_cache: dict[tuple[str, str, str, str, int], _BarBatch] = {}

        for record_id in ids:
            analysis = by_id.get(record_id)
            if analysis is None:
                skipped += 1
                continue
            processed += 1
            try:
                evaluated, counts = self._evaluate_analysis_record(
                    analysis=analysis,
                    engine_version=engine_version,
                    eval_config=eval_config,
                    bars_cache=bars_cache,
                    plan_types=selected_plan_types or None,
                    existing_keys=existing_keys,
                )
                if not evaluated:
                    skipped += 1
                    continue
                completed += counts["completed"]
                insufficient += counts["insufficient"]
                skipped += counts["skipped"]
                errors += counts["errors"]
                results.extend(evaluated)
            except Exception as exc:
                errors += 1
                logger.warning("指定 BTC 回测失败: %s#%s: %s", analysis.code, analysis.id, exc)

        saved = 0
        if results:
            saved = self.repo.save_results_batch(results, replace_existing=force)
            self._recompute_summaries(engine_version=engine_version)

        return {
            "processed": processed,
            "saved": saved,
            "completed": completed,
            "insufficient": insufficient,
            "skipped": skipped,
            "errors": errors,
        }

    def get_history_records(
        self,
        *,
        code: Optional[str] = None,
        analysis_mode: Optional[str] = None,
        direction: Optional[str] = None,
        plan_type: Optional[str] = None,
        result_status: Optional[str] = None,
        page: int = 1,
        limit: int = 20,
    ) -> dict[str, Any]:
        engine_version = self._engine_version()
        needs_plan_filter = any((analysis_mode, direction, plan_type, result_status))
        fetch_limit = 10000 if needs_plan_filter else int(limit)
        fetch_offset = 0 if needs_plan_filter else max(page - 1, 0) * int(limit)
        rows, total = self.repo.get_history_records(
            code=code,
            offset=fetch_offset,
            limit=fetch_limit,
        )
        result_rows = self.repo.get_results_for_history_ids(
            analysis_history_ids=[int(row.id) for row in rows if row.id is not None],
            engine_version=engine_version,
        )
        results_by_key: dict[tuple[int, str], CryptoBacktestResult] = {}
        for result in result_rows:
            key = (int(result.analysis_history_id), str(result.plan_type))
            if key not in results_by_key:
                results_by_key[key] = result

        items = [
            self._history_record_to_dict(
                row,
                results_by_key,
                analysis_mode_filter=analysis_mode,
                direction_filter=direction,
                plan_type_filter=plan_type,
                result_status_filter=result_status,
            )
            for row in rows
        ]
        items = [item for item in items if item is not None]
        filtered_total = len(items) if needs_plan_filter else total
        if needs_plan_filter:
            offset = max(page - 1, 0) * int(limit)
            items = items[offset:offset + int(limit)]

        return {
            "total": filtered_total,
            "page": page,
            "limit": limit,
            "items": items,
        }

    def get_history_record(self, analysis_history_id: int) -> Optional[dict[str, Any]]:
        engine_version = self._engine_version()
        rows, _total = self.repo.get_history_records(
            ids=[int(analysis_history_id)],
            offset=0,
            limit=1,
        )
        if not rows:
            return None

        result_rows = self.repo.get_results_for_history_ids(
            analysis_history_ids=[int(rows[0].id)],
            engine_version=engine_version,
        )
        results_by_key: dict[tuple[int, str], CryptoBacktestResult] = {}
        for result in result_rows:
            key = (int(result.analysis_history_id), str(result.plan_type))
            if key not in results_by_key:
                results_by_key[key] = result

        return self._history_record_to_dict(rows[0], results_by_key)

    def get_recent_evaluations(
        self,
        *,
        code: Optional[str] = None,
        horizon: Optional[str] = None,
        plan_type: Optional[str] = None,
        direction: Optional[str] = None,
        result_status: Optional[str] = None,
        page: int = 1,
        limit: int = 50,
    ) -> dict[str, Any]:
        engine_version = self._engine_version()
        rows, total = self.repo.get_results_paginated(
            code=normalize_crypto_symbol(code) if code else None,
            horizon=horizon,
            plan_type=plan_type,
            direction=direction,
            result_status=result_status,
            engine_version=engine_version,
            offset=max(page - 1, 0) * int(limit),
            limit=int(limit),
        )
        return {
            "total": total,
            "page": page,
            "limit": limit,
            "items": [self._result_to_dict(row) for row in rows],
        }

    def get_loss_review(
        self,
        *,
        code: Optional[str] = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Review net-loss trades using the current engine's stored evidence only."""
        engine_version = self._engine_version()
        rows = self.repo.list_results(
            code=normalize_crypto_symbol(code) if code else None,
            engine_version=engine_version,
            net_loss_only=True,
            limit=max(1, min(int(limit), 200)),
        )
        items = [self._loss_review_item(row) for row in rows]

        cause_breakdown: dict[str, int] = {}
        indicator_counts: dict[tuple[str, str], int] = {}
        for item in items:
            cause = str(item["primary_cause"])
            cause_breakdown[cause] = cause_breakdown.get(cause, 0) + 1
            for dimension, key in self._loss_indicator_values(item["indicator_tags"]):
                count_key = (dimension, key)
                indicator_counts[count_key] = indicator_counts.get(count_key, 0) + 1

        indicator_patterns = [
            {
                "dimension": dimension,
                "key": key,
                "loss_count": count,
                "note": "仅表示亏损样本中的共同特征，不代表已证明的因果关系。",
            }
            for (dimension, key), count in sorted(
                indicator_counts.items(), key=lambda item: (-item[1], item[0])
            )[:5]
        ]

        return {
            "engine_version": engine_version,
            "reviewed_results": len(rows),
            "loss_count": len(items),
            "cause_breakdown": cause_breakdown,
            "indicator_patterns": indicator_patterns,
            "improvement_suggestions": self._loss_review_suggestions(cause_breakdown, indicator_patterns),
            "items": items,
        }

    def get_summary(
        self,
        *,
        scope: str = "overall",
        code: Optional[str] = None,
        horizon: Optional[str] = None,
        plan_type: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        engine_version = self._engine_version()
        summary = self.repo.get_summary(
            scope=scope,
            code=normalize_crypto_symbol(code) if code else None,
            horizon=horizon,
            plan_type=plan_type,
            engine_version=engine_version,
        )
        if summary is None:
            self._recompute_summaries(engine_version=engine_version)
            summary = self.repo.get_summary(
                scope=scope,
                code=normalize_crypto_symbol(code) if code else None,
                horizon=horizon,
                plan_type=plan_type,
                engine_version=engine_version,
            )
        if summary is None:
            return None
        return self._summary_to_dict(summary)

    @classmethod
    def _loss_review_item(cls, row: CryptoBacktestResult) -> dict[str, Any]:
        diagnostics = parse_json_field(row.diagnostics_json)
        diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
        trade = diagnostics.get("trade")
        trade = trade if isinstance(trade, dict) else {}
        indicator_tags = diagnostics.get("indicator_tags")
        indicator_tags = indicator_tags if isinstance(indicator_tags, dict) else {}

        gross_pnl = cls._optional_float(trade.get("gross_pnl"))
        net_pnl = cls._optional_float(trade.get("net_pnl"))
        total_fee = cls._optional_float(trade.get("total_fee"))
        funding_cost = cls._optional_float(trade.get("funding_cost"))
        exit_reason = str(row.simulated_exit_reason or "")

        if gross_pnl is not None and net_pnl is not None and gross_pnl >= 0 and net_pnl < 0:
            primary_cause = "costs_exceeded_gross_profit"
            cause_group = "execution"
            confidence = "high"
            title = "费用与资金费吞没毛利"
            explanation = "价格路径没有产生毛亏损，但手续费、滑点或资金费使净收益转负。"
            evidence = [
                f"毛 PnL {gross_pnl:.2f}，净 PnL {net_pnl:.2f}",
                f"总费用 {total_fee:.2f}" if total_fee is not None else "总费用数据缺失",
            ]
            if funding_cost is not None:
                evidence.append(f"资金费 {funding_cost:.2f}")
            improvement = "提高最小目标收益阈值，要求目标收益覆盖双边费用、滑点和预估资金费。"
        elif exit_reason in {"stop_loss", "ambiguous_stop_loss", "liquidation"} or row.hit_stop_loss:
            primary_cause = "risk_control_exit"
            cause_group = "methodology"
            confidence = "high"
            title = "止损或强平退出"
            explanation = "交易触及预设风险边界，说明入场后的价格路径与计划方向不符。"
            evidence = [
                f"退出原因：{exit_reason or 'stop_loss'}",
                f"止损价 {cls._price_text(row.stop_loss)}，模拟入场价 {cls._price_text(row.simulated_entry_price)}",
            ]
            improvement = "复查该类信号的入场确认、止损距离和仓位；将相同指标组合与盈利样本对比后再调整阈值。"
        elif row.direction_correct is False:
            primary_cause = "direction_mismatch"
            cause_group = "methodology"
            confidence = "medium"
            title = "方向判断与后续走势不一致"
            explanation = "回测窗口内的实际价格方向没有支持该交易计划。"
            evidence = [
                f"计划方向：{row.direction or 'unknown'}",
                f"窗口收益 {cls._percent_text(row.simulated_return_pct)}，窗口结束价 {cls._price_text(row.end_close)}",
            ]
            improvement = "针对该指标组合提高确认门槛，优先检查量能确认、多周期方向一致性和关键位突破有效性。"
        else:
            primary_cause = "target_not_reached_before_exit"
            cause_group = "market_path"
            confidence = "medium"
            title = "持有期内未达到目标并以亏损退出"
            explanation = "交易未触发止损，但在最长持有期或窗口结束前未形成足够的有利价格路径。"
            evidence = [
                f"退出原因：{exit_reason or 'window_end'}",
                f"最高价 {cls._price_text(row.max_high)}，最低价 {cls._price_text(row.min_low)}，净收益 {cls._percent_text(row.simulated_return_pct)}",
            ]
            improvement = "检查目标位是否与波动率和最长持有期匹配；必要时增加趋势衰减退出或延长评估窗口后再比较。"

        return {
            "analysis_history_id": int(row.analysis_history_id),
            "code": row.code,
            "plan_type": row.plan_type,
            "horizon": row.horizon,
            "direction": row.direction,
            "analysis_created_at": row.analysis_created_at.isoformat() if row.analysis_created_at else None,
            "simulated_return_pct": row.simulated_return_pct,
            "net_pnl": net_pnl,
            "primary_cause": primary_cause,
            "cause_group": cause_group,
            "confidence": confidence,
            "title": title,
            "explanation": explanation,
            "evidence": evidence,
            "improvement": improvement,
            "external_context": cls._external_context(indicator_tags),
            "indicator_tags": indicator_tags,
        }

    @staticmethod
    def _optional_float(value: Any) -> Optional[float]:
        if isinstance(value, (int, float)):
            return float(value)
        return None

    @staticmethod
    def _price_text(value: Optional[float]) -> str:
        return f"{float(value):.2f}" if value is not None else "--"

    @staticmethod
    def _percent_text(value: Optional[float]) -> str:
        return f"{float(value):.2f}%" if value is not None else "--"

    @staticmethod
    def _external_context(indicator_tags: dict[str, Any]) -> str:
        macro = indicator_tags.get("macro_correlation")
        macro = macro if isinstance(macro, dict) else {}
        if macro.get("data_quality") == "available":
            return "该记录包含宏观关联指标，但未保存可直接证明外部事件导致亏损的事件证据。"
        return "该记录没有与交易时点绑定的宏观或新闻冲击证据，不能归因于外部因素。"

    @staticmethod
    def _loss_indicator_values(indicator_tags: dict[str, Any]) -> list[tuple[str, str]]:
        dimensions = (
            ("price_action", "state"),
            ("ema", "structure"),
            ("vwap", "price_position"),
            ("volume", "confirmation"),
            ("intraday", "alignment"),
            ("event", "type"),
        )
        values: list[tuple[str, str]] = []
        for dimension, field in dimensions:
            nested = indicator_tags.get(dimension)
            value = nested.get(field) if isinstance(nested, dict) else None
            if value not in (None, "", "unknown"):
                values.append((dimension, str(value)))
        return values

    @staticmethod
    def _loss_review_suggestions(
        cause_breakdown: dict[str, int],
        indicator_patterns: list[dict[str, Any]],
    ) -> list[str]:
        suggestions: list[str] = []
        if cause_breakdown.get("costs_exceeded_gross_profit"):
            suggestions.append("将费用、滑点和资金费纳入最小目标收益门槛，避免毛利为正但净收益为负。")
        if cause_breakdown.get("risk_control_exit") or cause_breakdown.get("direction_mismatch"):
            suggestions.append("按指标组合与周期拆分亏损样本，验证量能、多周期方向和关键位确认是否需要收紧。")
        if cause_breakdown.get("target_not_reached_before_exit"):
            suggestions.append("评估目标位、ATR 波动率和最长持有期是否匹配，避免交易在有效期内缺少足够价格空间。")
        low_volume = next(
            (
                item for item in indicator_patterns
                if item.get("dimension") == "volume" and item.get("key") == "low"
            ),
            None,
        )
        if low_volume:
            suggestions.append("低量能出现在亏损样本中，建议把量能确认作为入场前置条件，并与盈利样本对照验证。")
        return suggestions or ["当前没有可归因的净亏损成交；待样本积累后再评估分析方法。"]

    def delete_result(
        self,
        *,
        analysis_history_id: int,
        plan_type: str,
    ) -> dict[str, int]:
        engine_version = self._engine_version()
        deleted, _code = self.repo.delete_result(
            analysis_history_id=int(analysis_history_id),
            plan_type=plan_type,
            engine_version=engine_version,
        )
        if deleted:
            self._recompute_summaries(engine_version=engine_version)
        return {"deleted": int(deleted)}

    @staticmethod
    def _eval_config(config: Any, engine_version: str) -> CryptoPlanBacktestConfig:
        return CryptoPlanBacktestConfig(
            neutral_band_pct=float(getattr(config, "crypto_backtest_neutral_band_pct", 0.2)),
            engine_version=engine_version,
            initial_equity=float(getattr(config, "crypto_backtest_initial_equity", 10000.0)),
            risk_per_trade_pct=float(getattr(config, "crypto_backtest_risk_per_trade_pct", 1.0)),
            max_notional_pct=float(getattr(config, "crypto_backtest_max_notional_pct", 100.0)),
            leverage=float(getattr(config, "crypto_backtest_leverage", 1.0)),
            fee_rate_bps=float(getattr(config, "crypto_backtest_fee_rate_bps", 5.0)),
            slippage_bps=float(getattr(config, "crypto_backtest_slippage_bps", 2.0)),
            maker_fee_rate_bps=float(getattr(config, "crypto_backtest_maker_fee_rate_bps", 2.0)),
            taker_fee_rate_bps=float(getattr(config, "crypto_backtest_taker_fee_rate_bps", 5.0)),
            maintenance_margin_rate=float(getattr(config, "crypto_backtest_maintenance_margin_rate", 0.005)),
            minimum_risk_reward=float(getattr(config, "crypto_backtest_minimum_risk_reward", 1.2)),
            minimum_volume_ratio=float(getattr(config, "crypto_backtest_minimum_volume_ratio", 1.0)),
        )

    def _evaluate_analysis_record(
        self,
        *,
        analysis: AnalysisHistory,
        engine_version: str,
        eval_config: CryptoPlanBacktestConfig,
        bars_cache: dict[tuple[str, str, str, str, int], _BarBatch],
        plan_types: Optional[set[str]] = None,
        existing_keys: Optional[set[tuple[int, str]]] = None,
    ) -> tuple[list[CryptoBacktestResult], dict[str, int]]:
        plans = self._extract_plans(analysis)
        if plan_types:
            plans = [plan for plan in plans if plan.plan_type in plan_types]
        if existing_keys:
            plans = [
                plan
                for plan in plans
                if (int(analysis.id), str(plan.plan_type)) not in existing_keys
            ]
        if not plans:
            return [], {"completed": 0, "insufficient": 0, "skipped": 0, "errors": 0}

        analysis_at = analysis.created_at or datetime.now()
        market_analysis_at = self._local_naive_to_utc_naive(analysis_at)
        fetch_days = max(30, (datetime.now() - analysis_at).days + 30)
        results: list[CryptoBacktestResult] = []
        counts = {"completed": 0, "insufficient": 0, "skipped": 0, "errors": 0}

        for plan in plans:
            contract_window_bars = self._contract_window_bars(plan)
            normalized_contract, _contract_errors = CryptoBacktestEngine._validated_contract(
                plan.execution_contract,
                direction=plan.direction,
            )
            instrument_contract = normalized_contract.get("instrument") or resolve_crypto_instrument(
                "BTC-USDT-PERP",
                default_type="perpetual",
                venue=str(getattr(get_config(), "crypto_trading_exchange", "okx") or "okx"),
                margin_mode="isolated",
            ).to_contract()
            if plan.horizon == "intraday":
                batch = self._get_cached_bars(
                    bars_cache,
                    analysis.code,
                    period="hourly",
                    days=max(fetch_days, 3),
                    instrument=instrument_contract,
                )
                window_start = market_analysis_at
                window_end = market_analysis_at + timedelta(hours=contract_window_bars or 24)
            else:
                batch = self._get_cached_bars(
                    bars_cache,
                    analysis.code,
                    period="daily",
                    days=max(fetch_days, 5),
                    instrument=instrument_contract,
                )
                window_start = datetime.combine(market_analysis_at.date() + timedelta(days=1), time.min)
                window_end = window_start + timedelta(days=contract_window_bars or 1)

            forward_bars = [
                bar
                for bar in batch.bars
                if window_start <= bar.timestamp < window_end
            ]
            lookahead_guard = self._lookahead_guard(
                analysis_at=market_analysis_at,
                window_start=window_start,
                window_end=window_end,
                forward_bars=forward_bars,
            )
            if lookahead_guard["passed"]:
                evaluation = CryptoBacktestEngine.evaluate_plan(
                    plan=plan,
                    forward_bars=forward_bars,
                    config=eval_config,
                    evaluation_complete=self._evaluation_window_complete(
                        window_end=window_end,
                        data_snapshot=batch.metadata,
                    ),
                )
            else:
                evaluation = self._skipped_evaluation(plan, "lookahead_bias_detected")
            evaluation = self._attach_run_diagnostics(
                evaluation=evaluation,
                data_snapshot=batch.metadata,
                lookahead_guard=lookahead_guard,
            )
            evaluation = self._attach_indicator_tags(
                evaluation=evaluation,
                analysis=analysis,
                plan=plan,
            )
            status = evaluation.get("eval_status")
            if status == "completed":
                counts["completed"] += 1
            elif status == "insufficient_data":
                counts["insufficient"] += 1
            elif status == "skipped":
                counts["skipped"] += 1
            else:
                counts["errors"] += 1

            results.append(
                self._build_result_model(
                    analysis=analysis,
                    plan=plan,
                    evaluation=evaluation,
                    engine_version=engine_version,
                    evaluation_start=window_start,
                    evaluation_end=window_end,
                )
            )

        return results, counts

    @staticmethod
    def _contract_window_bars(plan: CryptoPlan) -> Optional[int]:
        contract, errors = CryptoBacktestEngine._validated_contract(
            plan.execution_contract,
            direction=plan.direction,
        )
        if errors:
            return None
        entry = contract["entry"]
        exit_config = contract["exit"]
        return (
            int(entry["max_wait_bars"])
            + int(entry["confirmation_bars"])
            + 1
            + int(exit_config["max_holding_bars"])
        )

    def _extract_plans(self, analysis: AnalysisHistory) -> list[CryptoPlan]:
        if not is_crypto_code(analysis.code):
            return []

        raw = parse_json_field(analysis.raw_result)
        if not isinstance(raw, dict):
            return []

        plans = extract_directional_strategy_plans(raw)
        analysis_mode = self._analysis_mode_from_snapshot(analysis)
        include_daily_plans = analysis_mode != "hourly"
        extracted: list[CryptoPlan] = []
        if include_daily_plans and plans.get("long_plan"):
            extracted.append(self._plan_from_payload("daily_long", "daily", "long", plans["long_plan"]))
        if include_daily_plans and plans.get("short_plan"):
            extracted.append(self._plan_from_payload("daily_short", "daily", "short", plans["short_plan"]))
        if plans.get("intraday_plan"):
            intraday_plan = plans["intraday_plan"] or {}
            direction = self._normalize_direction(intraday_plan.get("direction"))
            raw_enabled = intraday_plan.get("enabled")
            if raw_enabled is not None and not self._parse_enabled(raw_enabled):
                direction = "wait"
            elif direction not in {"long", "short"}:
                direction = "wait"
            extracted.append(self._plan_from_payload("intraday", "intraday", direction, intraday_plan))
        return extracted

    @staticmethod
    def _analysis_mode_from_snapshot(analysis: AnalysisHistory) -> str:
        snapshot = parse_json_field(getattr(analysis, "context_snapshot", None))
        if not isinstance(snapshot, dict):
            return "daily"
        mode = snapshot.get("analysis_mode")
        if not mode:
            enhanced_context = snapshot.get("enhanced_context")
            if isinstance(enhanced_context, dict):
                mode = enhanced_context.get("analysis_mode")
        normalized = str(mode or "daily").strip().lower()
        return normalized if normalized in {"daily", "hourly"} else "daily"

    @staticmethod
    def _plan_from_payload(plan_type: str, horizon: str, direction: str, payload: dict[str, Any]) -> CryptoPlan:
        execution_contract = payload.get("execution_contract")
        return CryptoPlan(
            plan_type=plan_type,
            horizon=horizon,
            direction=direction,
            entry_price=parse_sniper_value(payload.get("entry_price")),
            stop_loss=parse_sniper_value(payload.get("stop_loss")),
            take_profit=parse_sniper_value(payload.get("take_profit")),
            raw_plan=dict(payload),
            execution_contract=dict(execution_contract) if isinstance(execution_contract, dict) else None,
        )

    @staticmethod
    def _normalize_direction(value: Any) -> str:
        text = str(value or "").strip().lower()
        if text in {"long", "多", "多单", "做多", "buy"}:
            return "long"
        if text in {"short", "空", "空单", "做空", "sell"}:
            return "short"
        return "wait"

    @staticmethod
    def _parse_enabled(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        if not text:
            return False
        return text not in {"0", "false", "no", "off", "否", "关闭", "不启用"}

    def _get_cached_bars(
        self,
        cache: dict[tuple[str, str, str, str, int], _BarBatch],
        code: str,
        *,
        period: str,
        days: int,
        instrument: dict[str, Any],
    ) -> _BarBatch:
        normalized_code = normalize_crypto_symbol(code) or code
        instrument_type = str(instrument.get("type") or "perpetual")
        venue = str(instrument.get("venue") or "okx")
        key = (normalized_code, instrument_type, venue, period, int(days))
        if key not in cache:
            fetched_at = datetime.now(timezone.utc)
            if instrument_type == "perpetual":
                df = self.fetcher.get_perpetual_kline_data(
                    normalized_code,
                    period=period,
                    days=int(days),
                    venue=venue,
                    margin_mode=str(instrument.get("margin_mode") or "isolated"),
                )
            else:
                df = self.fetcher.get_kline_data(normalized_code, period=period, days=int(days))
            bars = self._bars_from_dataframe(df, period=period)
            bars = [
                bar
                for bar in bars
                if self._bar_is_closed(bar.timestamp, period=period, fetched_at=fetched_at)
            ]
            cache[key] = _BarBatch(
                bars=bars,
                metadata=self._kline_metadata(
                    code=normalized_code,
                    period=period,
                    days=int(days),
                    fetched_at=fetched_at,
                    bars=bars,
                    dataframe_attrs=dict(df.attrs),
                ),
            )
        return cache[key]

    @staticmethod
    def _bar_is_closed(timestamp: datetime, *, period: str, fetched_at: datetime) -> bool:
        fetched_at_utc = fetched_at.astimezone(timezone.utc).replace(tzinfo=None)
        duration = timedelta(days=1) if period == "daily" else timedelta(hours=1)
        return timestamp + duration <= fetched_at_utc

    @staticmethod
    def _bars_from_dataframe(df: pd.DataFrame, *, period: str) -> list[_Bar]:
        if df is None or df.empty:
            return []
        raw_bars: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            timestamp = CryptoBacktestService._parse_bar_timestamp(row.get("date"), period=period)
            open_price = CryptoBacktestService._safe_float(row.get("open"))
            high = CryptoBacktestService._safe_float(row.get("high"))
            low = CryptoBacktestService._safe_float(row.get("low"))
            close = CryptoBacktestService._safe_float(row.get("close"))
            volume = CryptoBacktestService._safe_float(row.get("volume"))
            if timestamp is None or open_price is None or high is None or low is None or close is None:
                continue
            raw_bars.append(
                {
                    "timestamp": timestamp,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                    "execution_open": CryptoBacktestService._safe_float(row.get("execution_open")),
                    "execution_high": CryptoBacktestService._safe_float(row.get("execution_high")),
                    "execution_low": CryptoBacktestService._safe_float(row.get("execution_low")),
                    "execution_close": CryptoBacktestService._safe_float(row.get("execution_close")),
                    "mark_open": CryptoBacktestService._safe_float(row.get("mark_open")),
                    "mark_high": CryptoBacktestService._safe_float(row.get("mark_high")),
                    "mark_low": CryptoBacktestService._safe_float(row.get("mark_low")),
                    "mark_close": CryptoBacktestService._safe_float(row.get("mark_close")),
                    "funding_rates": CryptoBacktestService._funding_rates(row.get("funding_rates")),
                    "funding_complete": bool(row.get("funding_complete", False)),
                }
            )

        bars: list[_Bar] = []
        for index, item in enumerate(raw_bars):
            lookback = raw_bars[max(0, index - 19):index + 1]
            prior = raw_bars[max(0, index - 20):index]
            prior_volumes = [float(bar["volume"]) for bar in prior if bar["volume"] is not None]
            volume_ratio = None
            if item["volume"] is not None and prior_volumes:
                average_volume = sum(prior_volumes) / len(prior_volumes)
                volume_ratio = float(item["volume"]) / average_volume if average_volume > 0 else None
            weighted = [
                (((float(bar["high"]) + float(bar["low"]) + float(bar["close"])) / 3), float(bar["volume"]))
                for bar in lookback
                if bar["volume"] is not None and float(bar["volume"]) > 0
            ]
            total_volume = sum(volume for _price, volume in weighted)
            vwap = sum(price * volume for price, volume in weighted) / total_volume if total_volume else None
            bars.append(
                _Bar(
                    timestamp=item["timestamp"],
                    open=float(item["open"]),
                    high=float(item["high"]),
                    low=float(item["low"]),
                    close=float(item["close"]),
                    volume=item["volume"],
                    volume_ratio=volume_ratio,
                    vwap=vwap,
                    execution_open=item["execution_open"],
                    execution_high=item["execution_high"],
                    execution_low=item["execution_low"],
                    execution_close=item["execution_close"],
                    mark_open=item["mark_open"],
                    mark_high=item["mark_high"],
                    mark_low=item["mark_low"],
                    mark_close=item["mark_close"],
                    funding_rates=item["funding_rates"],
                    funding_complete=bool(item["funding_complete"]),
                )
            )
        return bars

    @staticmethod
    def _evaluation_window_complete(*, window_end: datetime, data_snapshot: dict[str, Any]) -> bool:
        raw_fetched_at = data_snapshot.get("fetched_at")
        try:
            fetched_at = datetime.fromisoformat(str(raw_fetched_at)).astimezone(timezone.utc).replace(tzinfo=None)
        except (TypeError, ValueError):
            return False
        return fetched_at >= window_end

    @staticmethod
    def _parse_bar_timestamp(value: Any, *, period: str) -> Optional[datetime]:
        text = str(value or "").strip()
        if not text:
            return None
        formats = ["%Y-%m-%d %H:%M", "%Y-%m-%d"] if period != "daily" else ["%Y-%m-%d"]
        for fmt in formats:
            try:
                return datetime.strptime(text[:16] if "%H" in fmt else text[:10], fmt)
            except ValueError:
                continue
        return None

    @staticmethod
    def _local_naive_to_utc_naive(value: datetime) -> datetime:
        if value.tzinfo is not None and value.utcoffset() is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        local_tz = datetime.now().astimezone().tzinfo
        if local_tz is None:
            return value
        return value.replace(tzinfo=local_tz).astimezone(timezone.utc).replace(tzinfo=None)

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed == parsed else None

    @staticmethod
    def _funding_rates(value: Any) -> tuple[float, ...]:
        if not isinstance(value, (list, tuple)):
            return ()
        rates = []
        for item in value:
            parsed = CryptoBacktestService._safe_float(item)
            if parsed is not None:
                rates.append(parsed)
        return tuple(rates)

    @staticmethod
    def _kline_metadata(
        *,
        code: str,
        period: str,
        days: int,
        fetched_at: datetime,
        bars: list[_Bar],
        dataframe_attrs: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        attrs = dataframe_attrs or {}
        return {
            "source": attrs.get("source") or "CryptoFetcher",
            "venue": attrs.get("venue"),
            "instrument_type": attrs.get("instrument_type") or "spot",
            "canonical_symbol": attrs.get("canonical_symbol"),
            "market_symbol": attrs.get("market_symbol"),
            "price_type": attrs.get("price_type"),
            "funding_event_count": attrs.get("funding_event_count"),
            "funding_complete": attrs.get("funding_complete"),
            "code": code,
            "period": period,
            "requested_days": int(days),
            "fetched_at": fetched_at.isoformat(),
            "bar_count": len(bars),
            "range_start": bars[0].timestamp.isoformat() if bars else None,
            "range_end": bars[-1].timestamp.isoformat() if bars else None,
            "data_hash": CryptoBacktestService._bar_data_hash(bars),
        }

    @staticmethod
    def _bar_data_hash(bars: list[_Bar]) -> str:
        digest = hashlib.sha256()
        for bar in bars:
            digest.update(
                (
                    f"{bar.timestamp.isoformat()}|{float(bar.open if bar.open is not None else bar.close):.10g}|{bar.high:.10g}|"
                    f"{bar.low:.10g}|{bar.close:.10g}|{bar.volume}|{bar.volume_ratio}|{bar.vwap}|"
                    f"{bar.execution_open}|{bar.execution_high}|{bar.execution_low}|{bar.execution_close}|"
                    f"{bar.mark_open}|{bar.mark_high}|{bar.mark_low}|{bar.mark_close}|"
                    f"{bar.funding_rates}|{bar.funding_complete}\n"
                ).encode("utf-8")
            )
        return f"sha256:{digest.hexdigest()}"

    @staticmethod
    def _lookahead_guard(
        *,
        analysis_at: datetime,
        window_start: datetime,
        window_end: datetime,
        forward_bars: list[_Bar],
    ) -> dict[str, Any]:
        earliest_bar = min((bar.timestamp for bar in forward_bars), default=None)
        violations: list[str] = []
        if window_start < analysis_at:
            violations.append("window_starts_before_analysis")
        if window_end <= analysis_at:
            violations.append("window_ends_before_analysis")
        if earliest_bar is not None and earliest_bar < analysis_at:
            violations.append("bar_before_analysis")
        return {
            "passed": not violations,
            "analysis_at": analysis_at.isoformat(),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "earliest_bar": earliest_bar.isoformat() if earliest_bar else None,
            "violations": violations,
        }

    @staticmethod
    def _skipped_evaluation(plan: CryptoPlan, reason: str) -> dict[str, Any]:
        return {
            "plan_type": plan.plan_type,
            "horizon": plan.horizon,
            "direction": plan.direction,
            "entry_price": plan.entry_price,
            "stop_loss": plan.stop_loss,
            "take_profit": plan.take_profit,
            "execution_contract": plan.execution_contract,
            "eval_status": "skipped",
            "outcome": "skipped",
            "direction_correct": None,
            "entry_triggered": False,
            "diagnostics": {"reason": reason},
        }

    @staticmethod
    def _attach_run_diagnostics(
        *,
        evaluation: dict[str, Any],
        data_snapshot: dict[str, Any],
        lookahead_guard: dict[str, Any],
    ) -> dict[str, Any]:
        enriched = dict(evaluation)
        diagnostics = dict(enriched.get("diagnostics") or {})
        diagnostics["data_snapshot"] = data_snapshot
        diagnostics["lookahead_guard"] = lookahead_guard
        enriched["diagnostics"] = diagnostics
        return enriched

    @classmethod
    def _attach_indicator_tags(
        cls,
        *,
        evaluation: dict[str, Any],
        analysis: AnalysisHistory,
        plan: CryptoPlan,
    ) -> dict[str, Any]:
        enriched = dict(evaluation)
        diagnostics = dict(enriched.get("diagnostics") or {})
        diagnostics["indicator_tags"] = cls._indicator_tags_from_snapshot(analysis, plan)
        enriched["diagnostics"] = diagnostics
        return enriched

    @classmethod
    def _indicator_tags_from_snapshot(cls, analysis: AnalysisHistory, plan: CryptoPlan) -> dict[str, Any]:
        snapshot = parse_json_field(getattr(analysis, "context_snapshot", None))
        crypto_context = cls._crypto_context_from_snapshot(snapshot)
        timeframes = crypto_context.get("timeframes") if isinstance(crypto_context.get("timeframes"), dict) else {}
        daily_context = timeframes.get("daily") if isinstance(timeframes.get("daily"), dict) else crypto_context
        hourly_context = timeframes.get("hourly") if isinstance(timeframes.get("hourly"), dict) else {}
        source_context = hourly_context if plan.horizon == "intraday" and hourly_context else daily_context
        intraday = crypto_context.get("intraday") if isinstance(crypto_context.get("intraday"), dict) else {}
        derivatives = crypto_context.get("derivatives") if isinstance(crypto_context.get("derivatives"), dict) else {}
        funding = derivatives.get("funding") if isinstance(derivatives.get("funding"), dict) else {}
        open_interest = (
            derivatives.get("open_interest") if isinstance(derivatives.get("open_interest"), dict) else {}
        )
        funding_history = funding.get("history_7d") if isinstance(funding.get("history_7d"), dict) else {}
        oi_history = open_interest.get("history_24h") if isinstance(open_interest.get("history_24h"), dict) else {}
        basis = derivatives.get("basis") if isinstance(derivatives.get("basis"), dict) else {}
        long_short_ratio = derivatives.get("long_short_ratio") if isinstance(derivatives.get("long_short_ratio"), dict) else {}
        cross_exchange = derivatives.get("cross_exchange") if isinstance(derivatives.get("cross_exchange"), dict) else {}
        macro_correlation = crypto_context.get("macro_correlation") if isinstance(crypto_context.get("macro_correlation"), dict) else {}
        macro_assets = macro_correlation.get("assets") if isinstance(macro_correlation.get("assets"), dict) else {}
        nasdaq_macro = macro_assets.get("nasdaq") if isinstance(macro_assets.get("nasdaq"), dict) else {}
        dxy_macro = macro_assets.get("dxy") if isinstance(macro_assets.get("dxy"), dict) else {}
        us10y_macro = macro_assets.get("us10y") if isinstance(macro_assets.get("us10y"), dict) else {}
        gold_macro = macro_assets.get("gold") if isinstance(macro_assets.get("gold"), dict) else {}
        event = source_context.get("event") if isinstance(source_context.get("event"), dict) else {}
        volatility = source_context.get("volatility") if isinstance(source_context.get("volatility"), dict) else {}

        return {
            "tag_version": "btc-indicators-v1",
            "source_timeframe": "hourly" if source_context is hourly_context and hourly_context else "daily",
            "price_action": {
                "state": cls._tag_text(source_context, "price_action", "state"),
            },
            "ema": {
                "structure": cls._tag_text(source_context, "ema", "structure"),
            },
            "vwap": {
                "price_position": cls._tag_text(source_context, "vwap", "price_position"),
            },
            "volume": {
                "confirmation": cls._tag_text(source_context, "volume", "confirmation"),
            },
            "volatility": {
                "atr14_pct": cls._safe_float(volatility.get("atr14_pct")),
            },
            "intraday": {
                "alignment": cls._clean_tag_value(intraday.get("alignment")),
                "daily_bias": cls._clean_tag_value(intraday.get("daily_bias")),
                "hourly_bias": cls._clean_tag_value(intraday.get("hourly_bias")),
            },
            "event": {
                "type": cls._clean_tag_value(event.get("type")),
            },
            "derivatives": {
                "data_quality": cls._clean_tag_value(derivatives.get("data_quality")),
                "funding_state": cls._clean_tag_value(funding.get("state")),
                "funding_rate_pct": cls._safe_float(funding.get("rate_pct")),
                "funding_7d_trend": cls._clean_tag_value(funding_history.get("trend")),
                "funding_7d_avg_rate_pct": cls._safe_float(funding_history.get("avg_rate_pct")),
                "open_interest_state": cls._clean_tag_value(open_interest.get("state")),
                "open_interest_24h_state": cls._clean_tag_value(oi_history.get("state")),
                "open_interest_24h_change_pct": cls._safe_float(oi_history.get("change_pct")),
                "basis_state": cls._clean_tag_value(basis.get("state")),
                "basis_pct": cls._safe_float(basis.get("perpetual_premium_pct")),
                "long_short_state": cls._clean_tag_value(long_short_ratio.get("state")),
                "cross_exchange_quality": cls._clean_tag_value(cross_exchange.get("data_quality")),
                "leverage_pressure": cls._clean_tag_value(derivatives.get("leverage_pressure")),
            },
            "macro_correlation": {
                "data_quality": cls._clean_tag_value(macro_correlation.get("data_quality")),
                "nasdaq_state": cls._clean_tag_value(nasdaq_macro.get("state")),
                "nasdaq_30d": cls._safe_float(nasdaq_macro.get("correlation_30d")),
                "dxy_state": cls._clean_tag_value(dxy_macro.get("state")),
                "dxy_30d": cls._safe_float(dxy_macro.get("correlation_30d")),
                "us10y_state": cls._clean_tag_value(us10y_macro.get("state")),
                "gold_state": cls._clean_tag_value(gold_macro.get("state")),
            },
        }

    @staticmethod
    def _crypto_context_from_snapshot(snapshot: Any) -> dict[str, Any]:
        if not isinstance(snapshot, dict):
            return {}
        candidates = [
            snapshot.get("crypto_technical"),
        ]
        enhanced = snapshot.get("enhanced_context")
        if isinstance(enhanced, dict):
            candidates.append(enhanced.get("crypto_technical"))
        for candidate in candidates:
            if isinstance(candidate, dict):
                return candidate
        return {}

    @classmethod
    def _tag_text(cls, payload: dict[str, Any], *path: str) -> Optional[str]:
        current: Any = payload
        for key in path:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return cls._clean_tag_value(current)

    @staticmethod
    def _clean_tag_value(value: Any) -> Optional[str]:
        text = str(value or "").strip().lower()
        if not text or text in {"n/a", "none", "null", "unknown", "--"}:
            return None
        return text

    @staticmethod
    def _build_result_model(
        *,
        analysis: AnalysisHistory,
        plan: CryptoPlan,
        evaluation: dict[str, Any],
        engine_version: str,
        evaluation_start: datetime,
        evaluation_end: datetime,
    ) -> CryptoBacktestResult:
        return CryptoBacktestResult(
            analysis_history_id=int(analysis.id),
            code=normalize_crypto_symbol(analysis.code) or analysis.code,
            analysis_created_at=analysis.created_at,
            evaluated_at=datetime.now(),
            plan_type=plan.plan_type,
            horizon=plan.horizon,
            direction=plan.direction,
            engine_version=engine_version,
            eval_status=str(evaluation.get("eval_status") or "error"),
            evaluation_start=evaluation_start,
            evaluation_end=evaluation_end,
            entry_price=evaluation.get("entry_price"),
            stop_loss=evaluation.get("stop_loss"),
            take_profit=evaluation.get("take_profit"),
            entry_triggered=evaluation.get("entry_triggered"),
            entry_triggered_at=evaluation.get("entry_triggered_at"),
            start_price=evaluation.get("start_price"),
            end_close=evaluation.get("end_close"),
            max_high=evaluation.get("max_high"),
            min_low=evaluation.get("min_low"),
            direction_correct=evaluation.get("direction_correct"),
            outcome=evaluation.get("outcome"),
            hit_stop_loss=evaluation.get("hit_stop_loss"),
            hit_take_profit=evaluation.get("hit_take_profit"),
            first_hit=evaluation.get("first_hit"),
            first_hit_at=evaluation.get("first_hit_at"),
            first_hit_bars=evaluation.get("first_hit_bars"),
            simulated_entry_price=evaluation.get("simulated_entry_price"),
            simulated_exit_price=evaluation.get("simulated_exit_price"),
            simulated_exit_reason=evaluation.get("simulated_exit_reason"),
            simulated_return_pct=evaluation.get("simulated_return_pct"),
            raw_plan_json=json.dumps(plan.raw_plan, ensure_ascii=False, default=str),
            diagnostics_json=json.dumps(evaluation.get("diagnostics") or {}, ensure_ascii=False, default=str),
        )

    def _recompute_summaries(self, *, engine_version: str) -> None:
        rows = self.repo.list_results(engine_version=engine_version)
        self.repo.delete_summaries(engine_version=engine_version)
        summary_specs: list[tuple[str, Optional[str], Optional[str], Optional[str], list[CryptoBacktestResult]]] = [
            ("overall", None, None, None, rows),
        ]
        codes = sorted({row.code for row in rows if row.code})
        horizons = sorted({row.horizon for row in rows if row.horizon})
        plan_types = sorted({row.plan_type for row in rows if row.plan_type})

        summary_specs.extend(
            ("code", code, None, None, [row for row in rows if row.code == code])
            for code in codes
        )
        summary_specs.extend(
            ("horizon", None, horizon, None, [row for row in rows if row.horizon == horizon])
            for horizon in horizons
        )
        summary_specs.extend(
            ("plan_type", None, None, plan_type, [row for row in rows if row.plan_type == plan_type])
            for plan_type in plan_types
        )
        indicator_group_breakdown = self._indicator_group_breakdown(rows)

        for scope, code, horizon, plan_type, scoped_rows in summary_specs:
            data = CryptoBacktestEngine.compute_summary(
                results=scoped_rows,
                scope=scope,
                code=code,
                engine_version=engine_version,
            )
            diagnostics = {
                "risk_metrics": data.get("risk_metrics") or {},
                "equity_curve": data.get("equity_curve") or [],
                "sample_confidence": (data.get("diagnostics") or {}).get("sample_confidence") or {},
            }
            if scope == "overall":
                diagnostics["indicator_group_breakdown"] = indicator_group_breakdown
            self.repo.upsert_summary(
                CryptoBacktestSummary(
                    scope=scope,
                    code=code,
                    horizon=horizon,
                    plan_type=plan_type,
                    engine_version=engine_version,
                    computed_at=datetime.now(),
                    total_evaluations=data.get("total_evaluations") or 0,
                    completed_count=data.get("completed_count") or 0,
                    triggered_count=data.get("triggered_count") or 0,
                    no_entry_count=data.get("no_entry_count") or 0,
                    skipped_count=data.get("skipped_count") or 0,
                    insufficient_count=data.get("insufficient_count") or 0,
                    win_count=data.get("win_count") or 0,
                    loss_count=data.get("loss_count") or 0,
                    neutral_count=data.get("neutral_count") or 0,
                    direction_accuracy_pct=data.get("direction_accuracy_pct"),
                    win_rate_pct=data.get("win_rate_pct"),
                    avg_simulated_return_pct=data.get("avg_simulated_return_pct"),
                    plan_type_breakdown_json=json.dumps(
                        data.get("plan_type_breakdown") or {},
                        ensure_ascii=False,
                    ),
                    diagnostics_json=json.dumps(diagnostics, ensure_ascii=False, default=str),
                )
            )

    @staticmethod
    def _result_to_dict(row: CryptoBacktestResult) -> dict[str, Any]:
        diagnostics = parse_json_field(row.diagnostics_json) or {}
        analysis_mode = horizon_to_analysis_mode(row.horizon)
        return {
            "analysis_history_id": row.analysis_history_id,
            "code": row.code,
            "analysis_created_at": row.analysis_created_at.isoformat() if row.analysis_created_at else None,
            "evaluated_at": row.evaluated_at.isoformat() if row.evaluated_at else None,
            "plan_type": row.plan_type,
            "horizon": row.horizon,
            "analysis_mode": analysis_mode,
            "analysis_timeframe": analysis_timeframe_label(analysis_mode),
            "direction": row.direction,
            "engine_version": row.engine_version,
            "eval_status": row.eval_status,
            "evaluation_start": row.evaluation_start.isoformat() if row.evaluation_start else None,
            "evaluation_end": row.evaluation_end.isoformat() if row.evaluation_end else None,
            "entry_price": row.entry_price,
            "stop_loss": row.stop_loss,
            "take_profit": row.take_profit,
            "entry_triggered": row.entry_triggered,
            "entry_triggered_at": row.entry_triggered_at.isoformat() if row.entry_triggered_at else None,
            "direction_correct": row.direction_correct,
            "outcome": row.outcome,
            "hit_stop_loss": row.hit_stop_loss,
            "hit_take_profit": row.hit_take_profit,
            "first_hit": row.first_hit,
            "first_hit_at": row.first_hit_at.isoformat() if row.first_hit_at else None,
            "simulated_return_pct": row.simulated_return_pct,
            "trade": diagnostics.get("trade") or {},
            "execution": diagnostics.get("execution") or {},
            "diagnostics": diagnostics,
        }

    @staticmethod
    def _summary_to_dict(row: CryptoBacktestSummary) -> dict[str, Any]:
        diagnostics = parse_json_field(row.diagnostics_json) or {}
        analysis_mode = horizon_to_analysis_mode(row.horizon) if row.horizon else None
        return {
            "scope": row.scope,
            "code": row.code,
            "horizon": row.horizon,
            "analysis_mode": analysis_mode,
            "analysis_timeframe": analysis_timeframe_label(analysis_mode) if analysis_mode else None,
            "plan_type": row.plan_type,
            "engine_version": row.engine_version,
            "computed_at": row.computed_at.isoformat() if row.computed_at else None,
            "total_evaluations": row.total_evaluations,
            "completed_count": row.completed_count,
            "triggered_count": row.triggered_count,
            "no_entry_count": row.no_entry_count,
            "skipped_count": row.skipped_count,
            "insufficient_count": row.insufficient_count,
            "win_count": row.win_count,
            "loss_count": row.loss_count,
            "neutral_count": row.neutral_count,
            "direction_accuracy_pct": row.direction_accuracy_pct,
            "win_rate_pct": row.win_rate_pct,
            "avg_simulated_return_pct": row.avg_simulated_return_pct,
            "plan_type_breakdown": parse_json_field(row.plan_type_breakdown_json) or {},
            "risk_metrics": diagnostics.get("risk_metrics") or {},
            "equity_curve": diagnostics.get("equity_curve") or [],
            "diagnostics": diagnostics,
        }

    @classmethod
    def _indicator_group_breakdown(cls, rows: list[CryptoBacktestResult]) -> dict[str, Any]:
        if any(str(row.engine_version).lower() in {"btc-plan-v3", "btc-plan-v4", "btc-plan-v5"} for row in rows):
            triggered = [
                row
                for row in rows
                if row.eval_status == "completed" and row.entry_triggered is True
            ]
            independent, _excluded = CryptoBacktestEngine._independent_triggered_rows(triggered)
            triggered_ids = {id(row) for row in triggered}
            independent_ids = {id(row) for row in independent}
            rows = [
                row
                for row in rows
                if id(row) not in triggered_ids or id(row) in independent_ids
            ]
        dimensions: list[tuple[str, str, Any]] = [
            ("plan_type", "计划类型", lambda row, _tags: getattr(row, "plan_type", None)),
            ("direction", "方向", lambda row, _tags: getattr(row, "direction", None)),
            ("intraday.alignment", "多周期对齐", lambda _row, tags: cls._nested_tag(tags, "intraday", "alignment")),
            ("price_action.state", "价格行为", lambda _row, tags: cls._nested_tag(tags, "price_action", "state")),
            ("vwap.price_position", "VWAP 状态", lambda _row, tags: cls._nested_tag(tags, "vwap", "price_position")),
            ("ema.structure", "EMA 结构", lambda _row, tags: cls._nested_tag(tags, "ema", "structure")),
            ("volume.confirmation", "量能确认", lambda _row, tags: cls._nested_tag(tags, "volume", "confirmation")),
            ("event.type", "事件类型", lambda _row, tags: cls._nested_tag(tags, "event", "type")),
        ]
        grouped: dict[str, list[dict[str, Any]]] = {}
        for dimension, label, getter in dimensions:
            buckets: dict[str, list[CryptoBacktestResult]] = {}
            for row in rows:
                tags = cls._row_indicator_tags(row)
                raw_key = getter(row, tags)
                key = cls._clean_tag_value(raw_key) or "unknown"
                buckets.setdefault(key, []).append(row)
            grouped[dimension] = [
                cls._group_bucket_metrics(
                    dimension=dimension,
                    dimension_label=label,
                    key=key,
                    rows=bucket_rows,
                )
                for key, bucket_rows in sorted(buckets.items())
            ]
        return {
            "minimum_sample_count": 100,
            "groups": grouped,
        }

    @classmethod
    def _group_bucket_metrics(
        cls,
        *,
        dimension: str,
        dimension_label: str,
        key: str,
        rows: list[CryptoBacktestResult],
    ) -> dict[str, Any]:
        data = CryptoBacktestEngine.compute_summary(
            results=rows,
            scope=dimension,
            code=None,
            engine_version=getattr(rows[0], "engine_version", BTC_PLAN_ENGINE_VERSION) if rows else BTC_PLAN_ENGINE_VERSION,
        )
        risk_metrics = data.get("risk_metrics") or {}
        sample_confidence = (data.get("diagnostics") or {}).get("sample_confidence") or {}
        return {
            "dimension": dimension,
            "dimension_label": dimension_label,
            "key": key,
            "total_evaluations": data.get("total_evaluations") or 0,
            "completed_count": data.get("completed_count") or 0,
            "triggered_count": data.get("triggered_count") or 0,
            "win_rate_pct": data.get("win_rate_pct"),
            "avg_simulated_return_pct": data.get("avg_simulated_return_pct"),
            "max_drawdown_pct": risk_metrics.get("max_drawdown_pct"),
            "avg_r_multiple": risk_metrics.get("avg_r_multiple"),
            "sample_confidence": sample_confidence,
        }

    @staticmethod
    def _row_indicator_tags(row: CryptoBacktestResult) -> dict[str, Any]:
        diagnostics = parse_json_field(getattr(row, "diagnostics_json", None)) or {}
        tags = diagnostics.get("indicator_tags") if isinstance(diagnostics, dict) else None
        return tags if isinstance(tags, dict) else {}

    @staticmethod
    def _nested_tag(payload: dict[str, Any], *path: str) -> Optional[str]:
        current: Any = payload
        for key in path:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return str(current) if current is not None else None

    def _history_record_to_dict(
        self,
        row: AnalysisHistory,
        results_by_key: dict[tuple[int, str], CryptoBacktestResult],
        *,
        analysis_mode_filter: Optional[str] = None,
        direction_filter: Optional[str] = None,
        plan_type_filter: Optional[str] = None,
        result_status_filter: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        raw = parse_json_field(row.raw_result)
        snapshot = parse_json_field(row.context_snapshot)
        analysis_mode = self._analysis_mode_from_snapshot(row)
        if analysis_mode_filter and analysis_mode != analysis_mode_filter:
            return None
        plans = self._extract_plans(row)
        plan_items = [
            self._history_plan_to_dict(
                analysis_history_id=int(row.id),
                plan=plan,
                result=results_by_key.get((int(row.id), plan.plan_type)),
            )
            for plan in plans
        ]
        plan_items = [
            item
            for item in plan_items
            if self._matches_plan_filters(
                item,
                direction_filter=direction_filter,
                plan_type_filter=plan_type_filter,
                result_status_filter=result_status_filter,
            )
        ]
        if any((direction_filter, plan_type_filter, result_status_filter)) and not plan_items:
            return None
        terminal_statuses = {"completed", "win", "loss", "neutral", "no_entry", "skipped"}
        if not plan_items:
            backtest_status = "no_plan"
        elif all(item["backtest_status"] == "skipped" for item in plan_items):
            backtest_status = "skipped"
        elif all(item["backtest_status"] in terminal_statuses for item in plan_items):
            backtest_status = "completed"
        elif any(item["backtest_status"] in {"completed", "no_entry", "win", "loss", "neutral"} for item in plan_items):
            backtest_status = "partial"
        elif any(item["backtestable"] for item in plan_items):
            backtest_status = "pending"
        else:
            backtest_status = "invalid_plan"

        return {
            "analysis_history_id": int(row.id),
            "query_id": row.query_id,
            "code": normalize_crypto_symbol(row.code) or row.code,
            "stock_name": row.name,
            "report_type": row.report_type,
            "analysis_created_at": row.created_at.isoformat() if row.created_at else None,
            "analysis_mode": analysis_mode,
            "analysis_timeframe": analysis_timeframe_label(analysis_mode),
            "analysis_summary": row.analysis_summary or (raw.get("analysis_summary") if isinstance(raw, dict) else None),
            "operation_advice": row.operation_advice,
            "trend_prediction": row.trend_prediction,
            "backtest_status": backtest_status,
            "plans": plan_items,
            "diagnostics": {
                "context_snapshot_available": isinstance(snapshot, dict),
            },
        }

    @staticmethod
    def _matches_plan_filters(
        item: dict[str, Any],
        *,
        direction_filter: Optional[str],
        plan_type_filter: Optional[str],
        result_status_filter: Optional[str],
    ) -> bool:
        if direction_filter and item.get("direction") != direction_filter:
            return False
        if plan_type_filter and item.get("plan_type") != plan_type_filter:
            return False
        if result_status_filter and item.get("backtest_status") != result_status_filter:
            return False
        return True

    def _history_plan_to_dict(
        self,
        *,
        analysis_history_id: int,
        plan: CryptoPlan,
        result: Optional[CryptoBacktestResult],
    ) -> dict[str, Any]:
        missing_fields = self._missing_plan_fields(plan)
        no_trade_plan = plan.direction == "wait" and not missing_fields
        backtestable = not missing_fields and plan.direction in {"long", "short"}
        latest_result = self._result_to_dict(result) if result else None
        if no_trade_plan:
            backtest_status = self._plan_result_status(result) if result else "skipped"
        elif not backtestable:
            backtest_status = "invalid_plan"
        elif result:
            backtest_status = self._plan_result_status(result)
        else:
            backtest_status = "pending"
        analysis_mode = horizon_to_analysis_mode(plan.horizon)
        return {
            "plan_type": plan.plan_type,
            "horizon": plan.horizon,
            "analysis_mode": analysis_mode,
            "analysis_timeframe": analysis_timeframe_label(analysis_mode),
            "direction": plan.direction,
            "entry_price": plan.entry_price,
            "stop_loss": plan.stop_loss,
            "take_profit": plan.take_profit,
            "execution_contract": plan.execution_contract,
            "invalid_condition": self._raw_plan_text(plan, "invalid_condition", "invalidation"),
            "risk_reward": self._raw_plan_text(plan, "risk_reward"),
            "position_hint": self._raw_plan_text(plan, "position_hint"),
            "confidence": self._raw_plan_text(plan, "confidence"),
            "backtestable": backtestable,
            "quality_status": (
                "executable_contract"
                if backtestable
                else "no_trade_plan"
                if no_trade_plan
                else "missing_required_fields"
            ),
            "missing_fields": missing_fields,
            "no_trade_reason": self._no_trade_reason(plan),
            "backtest_status": backtest_status,
            "latest_result": latest_result,
            "indicator_tags": (latest_result or {}).get("diagnostics", {}).get("indicator_tags") if latest_result else None,
        }

    def _missing_plan_fields(self, plan: CryptoPlan) -> list[str]:
        missing = []
        if plan.direction not in {"long", "short"}:
            raw_plan = plan.raw_plan or {}
            raw_direction = str(raw_plan.get("direction") or "").strip().lower()
            raw_enabled = raw_plan.get("enabled")
            explicitly_disabled = raw_enabled is not None and not self._parse_enabled(raw_enabled)
            if explicitly_disabled or raw_direction in {"wait", "neutral", "none", "观望", "等待"}:
                return missing
            missing.append("direction")
        if plan.entry_price is None:
            missing.append("entry_zone")
        if plan.stop_loss is None:
            missing.append("stop_loss")
        if plan.take_profit is None:
            missing.append("take_profit")
        if self._engine_version().strip().lower() in {"btc-plan-v3", "btc-plan-v4", "btc-plan-v5"}:
            _contract, contract_errors = CryptoBacktestEngine._validated_contract(
                plan.execution_contract,
                direction=plan.direction,
            )
            if contract_errors:
                missing.append("execution_contract")
        return missing

    @staticmethod
    def _no_trade_reason(plan: CryptoPlan) -> Optional[str]:
        raw = plan.raw_plan or {}
        for key in ("no_trade_reason", "reason", "invalidation"):
            value = raw.get(key)
            if value:
                return str(value)
        if plan.direction in {"long", "short"}:
            return None
        return "计划方向为观望，暂不回测入场交易。"

    @staticmethod
    def _raw_plan_text(plan: CryptoPlan, *keys: str) -> Optional[str]:
        raw = plan.raw_plan or {}
        for key in keys:
            value = raw.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return None

    @staticmethod
    def _plan_result_status(row: CryptoBacktestResult) -> str:
        if row.eval_status == "completed":
            return str(row.outcome or "completed")
        return str(row.eval_status or "pending")

    @staticmethod
    def _engine_version() -> str:
        return str(getattr(get_config(), "crypto_backtest_engine_version", BTC_PLAN_ENGINE_VERSION) or BTC_PLAN_ENGINE_VERSION)
