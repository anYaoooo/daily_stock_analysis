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

BTC_PLAN_ENGINE_VERSION = "btc-plan-v2"


@dataclass(frozen=True)
class _Bar:
    timestamp: datetime
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class _BarBatch:
    bars: list[_Bar]
    metadata: dict[str, Any]


class CryptoBacktestService:
    """Run and query BTC report plan backtests."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()
        self.repo = CryptoBacktestRepository(self.db)
        self.fetcher = CryptoFetcher()

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

        bars_cache: dict[tuple[str, str, int], _BarBatch] = {}
        eval_config = self._eval_config(config, engine_version)

        for analysis in candidates:
            processed += 1
            try:
                evaluated, counts = self._evaluate_analysis_record(
                    analysis=analysis,
                    engine_version=engine_version,
                    eval_config=eval_config,
                    bars_cache=bars_cache,
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
            }

        processed = 0
        completed = 0
        insufficient = 0
        skipped = 0
        errors = 0
        results: list[CryptoBacktestResult] = []
        bars_cache: dict[tuple[str, str, int], _BarBatch] = {}

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
            return None
        return self._summary_to_dict(summary)

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
        )

    def _evaluate_analysis_record(
        self,
        *,
        analysis: AnalysisHistory,
        engine_version: str,
        eval_config: CryptoPlanBacktestConfig,
        bars_cache: dict[tuple[str, str, int], _BarBatch],
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
        fetch_days = max(3, (datetime.now() - analysis_at).days + 3)
        results: list[CryptoBacktestResult] = []
        counts = {"completed": 0, "insufficient": 0, "skipped": 0, "errors": 0}

        for plan in plans:
            if plan.horizon == "intraday":
                batch = self._get_cached_bars(
                    bars_cache,
                    analysis.code,
                    period="hourly",
                    days=max(fetch_days, 3),
                )
                window_start = market_analysis_at
                window_end = market_analysis_at + timedelta(hours=24)
            else:
                batch = self._get_cached_bars(
                    bars_cache,
                    analysis.code,
                    period="daily",
                    days=max(fetch_days, 5),
                )
                window_start = datetime.combine(market_analysis_at.date() + timedelta(days=1), time.min)
                window_end = window_start + timedelta(days=1)

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
            enabled = self._parse_enabled(intraday_plan.get("enabled"))
            if not enabled or direction not in {"long", "short"}:
                direction = direction if direction in {"long", "short"} else "wait"
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
        return CryptoPlan(
            plan_type=plan_type,
            horizon=horizon,
            direction=direction,
            entry_price=parse_sniper_value(payload.get("entry_price")),
            stop_loss=parse_sniper_value(payload.get("stop_loss")),
            take_profit=parse_sniper_value(payload.get("take_profit")),
            raw_plan=dict(payload),
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
        cache: dict[tuple[str, str, int], _BarBatch],
        code: str,
        *,
        period: str,
        days: int,
    ) -> _BarBatch:
        normalized_code = normalize_crypto_symbol(code) or code
        key = (normalized_code, period, int(days))
        if key not in cache:
            fetched_at = datetime.now(timezone.utc)
            df = self.fetcher.get_kline_data(normalized_code, period=period, days=int(days))
            bars = self._bars_from_dataframe(df, period=period)
            cache[key] = _BarBatch(
                bars=bars,
                metadata=self._kline_metadata(
                    code=normalized_code,
                    period=period,
                    days=int(days),
                    fetched_at=fetched_at,
                    bars=bars,
                ),
            )
        return cache[key]

    @staticmethod
    def _bars_from_dataframe(df: pd.DataFrame, *, period: str) -> list[_Bar]:
        if df is None or df.empty:
            return []
        bars: list[_Bar] = []
        for _, row in df.iterrows():
            timestamp = CryptoBacktestService._parse_bar_timestamp(row.get("date"), period=period)
            high = CryptoBacktestService._safe_float(row.get("high"))
            low = CryptoBacktestService._safe_float(row.get("low"))
            close = CryptoBacktestService._safe_float(row.get("close"))
            if timestamp is None or high is None or low is None or close is None:
                continue
            bars.append(_Bar(timestamp=timestamp, high=high, low=low, close=close))
        return bars

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
    def _kline_metadata(
        *,
        code: str,
        period: str,
        days: int,
        fetched_at: datetime,
        bars: list[_Bar],
    ) -> dict[str, Any]:
        return {
            "source": "CryptoFetcher",
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
                f"{bar.timestamp.isoformat()}|{bar.high:.10g}|{bar.low:.10g}|{bar.close:.10g}\n".encode("utf-8")
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
            "minimum_sample_count": 30,
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
        terminal_statuses = {"completed", "win", "loss", "neutral", "no_entry", "skipped", "insufficient_data"}
        if not plan_items:
            backtest_status = "no_plan"
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
        backtestable = not missing_fields and plan.direction in {"long", "short"}
        latest_result = self._result_to_dict(result) if result else None
        if result:
            backtest_status = self._plan_result_status(result)
        elif backtestable:
            backtest_status = "pending"
        else:
            backtest_status = "invalid_plan"
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
            "invalid_condition": self._raw_plan_text(plan, "invalid_condition", "invalidation"),
            "risk_reward": self._raw_plan_text(plan, "risk_reward"),
            "position_hint": self._raw_plan_text(plan, "position_hint"),
            "confidence": self._raw_plan_text(plan, "confidence"),
            "backtestable": backtestable,
            "quality_status": "ok" if backtestable else "missing_required_fields",
            "missing_fields": missing_fields,
            "no_trade_reason": self._no_trade_reason(plan),
            "backtest_status": backtest_status,
            "latest_result": latest_result,
            "indicator_tags": (latest_result or {}).get("diagnostics", {}).get("indicator_tags") if latest_result else None,
        }

    @staticmethod
    def _missing_plan_fields(plan: CryptoPlan) -> list[str]:
        missing = []
        if plan.direction not in {"long", "short"}:
            missing.append("direction")
        if plan.entry_price is None:
            missing.append("entry_zone")
        if plan.stop_loss is None:
            missing.append("stop_loss")
        if plan.take_profit is None:
            missing.append("take_profit")
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
