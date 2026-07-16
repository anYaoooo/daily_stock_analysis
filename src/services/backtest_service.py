# -*- coding: utf-8 -*-
"""Backtest orchestration service."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, select

from src.config import get_config
from src.core.backtest_engine import OVERALL_SENTINEL_CODE, BacktestEngine, EvaluationConfig
from data_provider.crypto_fetcher import CryptoFetcher, is_crypto_code, normalize_crypto_symbol
from src.market_phase_summary import extract_market_phase_summary, normalize_analysis_phase_bucket
from src.repositories.backtest_repo import BacktestRepository
from src.repositories.stock_repo import StockRepository
from src.schemas.decision_action import build_action_fields
from src.storage import BacktestResult, BacktestSummary, DatabaseManager
from src.utils.data_processing import parse_json_field
from src.utils.timeframe import analysis_timeframe_label, normalize_analysis_mode

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _BacktestBar:
    timestamp: datetime
    date: date
    high: Optional[float]
    low: Optional[float]
    close: Optional[float]


class BacktestService:
    """Service layer to run and query backtests."""

    MAX_DYNAMIC_SUMMARY_ROWS = 2000

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()
        self.repo = BacktestRepository(self.db)
        self.stock_repo = StockRepository(self.db)

    def run_backtest(
        self,
        *,
        code: Optional[str] = None,
        force: bool = False,
        eval_window_days: Optional[int] = None,
        min_age_days: Optional[int] = None,
        analysis_mode: Optional[str] = None,
        limit: int = 200,
    ) -> Dict[str, Any]:
        config = get_config()

        if eval_window_days is None:
            eval_window_days = getattr(config, "backtest_eval_window_days", 10)
        if min_age_days is None:
            min_age_days = getattr(config, "backtest_min_age_days", 14)

        engine_version = getattr(config, "backtest_engine_version", "v1")
        neutral_band_pct = float(getattr(config, "backtest_neutral_band_pct", 2.0))

        eval_config = EvaluationConfig(
            eval_window_days=int(eval_window_days),
            neutral_band_pct=neutral_band_pct,
            engine_version=str(engine_version),
        )

        candidates = self.repo.get_candidates(
            code=code,
            min_age_days=int(min_age_days),
            limit=int(limit),
            eval_window_days=int(eval_window_days),
            engine_version=str(engine_version),
            analysis_mode=analysis_mode,
            force=force,
        )

        processed = 0
        completed = 0
        insufficient = 0
        errors = 0
        touched_codes: set[str] = set()

        results_to_save: List[BacktestResult] = []

        for analysis in candidates:
            record_analysis_mode = self._analysis_mode_from_snapshot(analysis)
            requested_mode = normalize_analysis_mode(analysis_mode) if analysis_mode else None
            if requested_mode is not None and record_analysis_mode != requested_mode:
                continue

            processed += 1
            touched_codes.add(analysis.code)

            try:
                analysis_date = self._resolve_analysis_date(analysis)
                if analysis_date is None:
                    errors += 1
                    results_to_save.append(
                        BacktestResult(
                            analysis_history_id=analysis.id,
                            code=analysis.code,
                            eval_window_days=int(eval_window_days),
                            engine_version=str(engine_version),
                            analysis_mode=record_analysis_mode,
                            eval_status="error",
                            evaluated_at=datetime.now(),
                            operation_advice=analysis.operation_advice,
                        )
                    )
                    continue

                eval_inputs = self._prepare_evaluation_inputs(
                    code=analysis.code,
                    analysis_date=analysis_date,
                    analysis_created_at=analysis.created_at,
                    analysis_mode=record_analysis_mode,
                    eval_window_days=int(eval_window_days),
                )

                if eval_inputs is None:
                    insufficient += 1
                    results_to_save.append(
                        BacktestResult(
                            analysis_history_id=analysis.id,
                            code=normalize_crypto_symbol(analysis.code) or analysis.code,
                            analysis_date=analysis_date,
                            eval_window_days=int(eval_window_days),
                            engine_version=str(engine_version),
                            analysis_mode=record_analysis_mode,
                            eval_status="insufficient_data",
                            evaluated_at=datetime.now(),
                            operation_advice=analysis.operation_advice,
                        )
                    )
                    continue

                evaluation = BacktestEngine.evaluate_single(
                    operation_advice=analysis.operation_advice,
                    analysis_date=eval_inputs["analysis_date"],
                    start_price=float(eval_inputs["start_price"]),
                    forward_bars=eval_inputs["forward_bars"],
                    stop_loss=analysis.stop_loss,
                    take_profit=analysis.take_profit,
                    config=eval_config,
                )

                status = evaluation.get("eval_status")
                if status == "insufficient_data":
                    insufficient += 1
                elif status == "completed":
                    completed += 1
                else:
                    errors += 1

                results_to_save.append(
                    BacktestResult(
                        analysis_history_id=analysis.id,
                        code=normalize_crypto_symbol(analysis.code) or analysis.code,
                        analysis_date=evaluation.get("analysis_date"),
                        eval_window_days=int(evaluation.get("eval_window_days") or eval_window_days),
                        engine_version=str(evaluation.get("engine_version") or engine_version),
                        analysis_mode=record_analysis_mode,
                        eval_status=str(evaluation.get("eval_status") or "error"),
                        evaluated_at=datetime.now(),
                        operation_advice=evaluation.get("operation_advice"),
                        position_recommendation=evaluation.get("position_recommendation"),
                        start_price=evaluation.get("start_price"),
                        end_close=evaluation.get("end_close"),
                        max_high=evaluation.get("max_high"),
                        min_low=evaluation.get("min_low"),
                        stock_return_pct=evaluation.get("stock_return_pct"),
                        direction_expected=evaluation.get("direction_expected"),
                        direction_correct=evaluation.get("direction_correct"),
                        outcome=evaluation.get("outcome"),
                        stop_loss=evaluation.get("stop_loss"),
                        take_profit=evaluation.get("take_profit"),
                        hit_stop_loss=evaluation.get("hit_stop_loss"),
                        hit_take_profit=evaluation.get("hit_take_profit"),
                        first_hit=evaluation.get("first_hit"),
                        first_hit_date=evaluation.get("first_hit_date"),
                        first_hit_trading_days=evaluation.get("first_hit_trading_days"),
                        simulated_entry_price=evaluation.get("simulated_entry_price"),
                        simulated_exit_price=evaluation.get("simulated_exit_price"),
                        simulated_exit_reason=evaluation.get("simulated_exit_reason"),
                        simulated_return_pct=evaluation.get("simulated_return_pct"),
                    )
                )

            except Exception as exc:
                errors += 1
                logger.error(f"回测失败: {analysis.code}#{analysis.id}: {exc}")
                results_to_save.append(
                    BacktestResult(
                        analysis_history_id=analysis.id,
                        code=analysis.code,
                        analysis_date=self._resolve_analysis_date(analysis),
                        eval_window_days=int(eval_window_days),
                        engine_version=str(engine_version),
                        analysis_mode=record_analysis_mode,
                        eval_status="error",
                        evaluated_at=datetime.now(),
                        operation_advice=analysis.operation_advice,
                    )
                )

        summary_codes_by_mode: Dict[str, set[str]] = {}
        for row in results_to_save:
            row_mode = row.analysis_mode or "daily"
            summary_codes_by_mode.setdefault(row_mode, set()).add(row.code)

        saved = 0
        if results_to_save:
            saved = self.repo.save_results_batch(results_to_save, replace_existing=force)

        if saved:
            for saved_mode, mode_codes in sorted(summary_codes_by_mode.items()):
                self._recompute_summaries(
                    touched_codes=sorted(mode_codes) or sorted(touched_codes),
                    eval_window_days=int(eval_window_days),
                    engine_version=str(engine_version),
                    analysis_mode=saved_mode,
                )

        return {
            "processed": processed,
            "saved": saved,
            "completed": completed,
            "insufficient": insufficient,
            "errors": errors,
        }

    def get_recent_evaluations(
        self,
        *,
        code: Optional[str],
        eval_window_days: Optional[int] = None,
        limit: int = 50,
        page: int = 1,
        analysis_date_from: Optional[date] = None,
        analysis_date_to: Optional[date] = None,
        analysis_phase: Optional[str] = None,
        analysis_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        config = get_config()
        engine_version = str(getattr(config, "backtest_engine_version", "v1"))
        normalized_mode = normalize_analysis_mode(analysis_mode) if analysis_mode else None

        phase_bucket = self._normalize_phase_filter(analysis_phase)
        if eval_window_days is None and (analysis_date_from is not None or analysis_date_to is not None or phase_bucket is not None):
            eval_window_days = self._infer_eval_window_for_query(
                code=code,
                engine_version=engine_version,
                analysis_date_from=analysis_date_from,
                analysis_date_to=analysis_date_to,
                analysis_mode=normalized_mode,
            )
        if phase_bucket is not None:
            return self._get_recent_evaluations_by_phase(
                code=code,
                eval_window_days=eval_window_days,
                engine_version=engine_version,
                analysis_mode=normalized_mode,
                limit=limit,
                page=page,
                analysis_date_from=analysis_date_from,
                analysis_date_to=analysis_date_to,
                phase_bucket=phase_bucket,
            )

        offset = max(page - 1, 0) * limit
        rows, total = self.repo.get_results_paginated(
            code=code,
            eval_window_days=eval_window_days,
            engine_version=engine_version,
            analysis_date_from=analysis_date_from,
            analysis_date_to=analysis_date_to,
            days=None,
            analysis_mode=normalized_mode,
            offset=offset,
            limit=limit,
        )
        items = []
        for result, stock_name, trend_prediction, _created_at, context_snapshot, raw_result, report_type in rows:
            summary = extract_market_phase_summary(context_snapshot)
            items.append(
                self._result_to_dict(
                    result,
                    stock_name,
                    trend_prediction,
                    market_phase_summary=summary,
                    market_phase=self._phase_bucket_from_summary(summary),
                    raw_result=raw_result,
                    report_type=report_type,
                )
            )
        return {"total": total, "page": page, "limit": limit, "items": items}

    def get_summary(
        self,
        *,
        scope: str,
        code: Optional[str],
        eval_window_days: Optional[int] = None,
        analysis_date_from: Optional[date] = None,
        analysis_date_to: Optional[date] = None,
        analysis_phase: Optional[str] = None,
        analysis_mode: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        config = get_config()
        engine_version = str(getattr(config, "backtest_engine_version", "v1"))
        lookup_code = OVERALL_SENTINEL_CODE if scope == "overall" else code
        normalized_mode = normalize_analysis_mode(analysis_mode) if analysis_mode else None

        phase_bucket = self._normalize_phase_filter(analysis_phase)
        if analysis_date_from is not None or analysis_date_to is not None or phase_bucket is not None or normalized_mode is not None:
            if eval_window_days is None:
                eval_window_days = self._infer_eval_window_for_query(
                    code=code,
                    engine_version=engine_version,
                    analysis_date_from=analysis_date_from,
                    analysis_date_to=analysis_date_to,
                    analysis_mode=normalized_mode,
                )
            ew = int(eval_window_days) if eval_window_days is not None else None
            count = self.repo.count_results(
                code=code,
                eval_window_days=ew,
                engine_version=engine_version,
                analysis_date_from=analysis_date_from,
                analysis_date_to=analysis_date_to,
                analysis_mode=normalized_mode,
            )
            if count > self.MAX_DYNAMIC_SUMMARY_ROWS:
                if phase_bucket is not None:
                    raise ValueError(
                        "Phase-filtered summary candidate set matches too many rows; "
                        "narrow the analysis date range, stock code, or evaluation window."
                    )
                raise ValueError("Date-filtered summary matches too many rows; narrow the analysis date range or stock code.")
            if phase_bucket is not None:
                rows_with_context = self.repo.list_results_with_context(
                    code=code,
                    eval_window_days=ew,
                    engine_version=engine_version,
                    analysis_date_from=analysis_date_from,
                    analysis_date_to=analysis_date_to,
                    analysis_mode=normalized_mode,
                    limit=self.MAX_DYNAMIC_SUMMARY_ROWS + 1,
                )
                if len(rows_with_context) > self.MAX_DYNAMIC_SUMMARY_ROWS:
                    raise ValueError(
                        "Phase-filtered summary matches too many rows; narrow the analysis date range or stock code."
                    )
                filtered_pairs = [
                    (row, snapshot)
                    for row, snapshot in rows_with_context
                    if self._phase_bucket_from_snapshot(snapshot) == phase_bucket
                ]
                phase_counts = self._phase_counts_from_contexts([snapshot for _, snapshot in filtered_pairs])
                filtered_rows = [row for row, _ in filtered_pairs]
                return self._build_dynamic_summary(
                    rows=filtered_rows,
                    scope=scope,
                    code=lookup_code,
                    eval_window_days=int(eval_window_days) if eval_window_days is not None else None,
                    engine_version=engine_version,
                    analysis_mode=normalized_mode,
                    max_rows=self.MAX_DYNAMIC_SUMMARY_ROWS,
                    phase_breakdown=phase_counts["phase_breakdown"],
                    raw_phase_counts=phase_counts["raw_phase_counts"],
                )
            rows = self.repo.list_results(
                code=code,
                eval_window_days=ew,
                engine_version=engine_version,
                analysis_date_from=analysis_date_from,
                analysis_date_to=analysis_date_to,
                analysis_mode=normalized_mode,
            )
            return self._build_dynamic_summary(
                rows=rows,
                scope=scope,
                code=lookup_code,
                eval_window_days=int(eval_window_days) if eval_window_days is not None else None,
                engine_version=engine_version,
                analysis_mode=normalized_mode,
                max_rows=self.MAX_DYNAMIC_SUMMARY_ROWS,
            )

        summary = self.repo.get_summary(
            scope=scope,
            code=lookup_code,
            eval_window_days=eval_window_days,
            engine_version=engine_version,
            analysis_mode=normalized_mode or "daily",
        )
        if summary is None:
            return None
        return self._summary_to_dict(summary)

    def get_global_summary(self, *, eval_window_days: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Return overall backtest metrics normalized for Agent memory consumers."""
        return self._normalize_learning_summary(
            self.get_summary(scope="overall", code=None, eval_window_days=eval_window_days)
        )

    def get_stock_summary(self, code: str, *, eval_window_days: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Return per-stock backtest metrics normalized for Agent memory consumers."""
        return self._normalize_learning_summary(
            self.get_summary(scope="stock", code=code, eval_window_days=eval_window_days)
        )

    def get_skill_summary(self, skill_id: str, *, eval_window_days: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Return skill-like summary metrics for Agent memory consumers.

        The current backtest storage layer only persists overall / per-stock rollups.
        Re-using the overall rollup here would fabricate skill-specific performance
        and mislead auto-weighting. Until real skill-tagged summaries exist, return
        ``None`` so downstream callers fall back to neutral weighting.
        """
        return None

    def get_strategy_summary(self, strategy_id: str, *, eval_window_days: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Compatibility wrapper for legacy strategy-based callers."""
        summary = self.get_skill_summary(strategy_id, eval_window_days=eval_window_days)
        if summary is None:
            return None
        normalized = dict(summary)
        normalized["strategy_id"] = strategy_id
        return normalized

    def delete_result(
        self,
        *,
        analysis_history_id: int,
        eval_window_days: int,
        analysis_mode: Optional[str] = None,
    ) -> Dict[str, int]:
        config = get_config()
        engine_version = str(getattr(config, "backtest_engine_version", "v1"))
        normalized_mode = normalize_analysis_mode(analysis_mode) if analysis_mode else "daily"
        deleted, code = self.repo.delete_result(
            analysis_history_id=int(analysis_history_id),
            eval_window_days=int(eval_window_days),
            engine_version=engine_version,
            analysis_mode=normalized_mode,
        )
        if deleted and code:
            self._recompute_summaries(
                touched_codes=[code],
                eval_window_days=int(eval_window_days),
                engine_version=engine_version,
                analysis_mode=normalized_mode,
            )
        return {"deleted": int(deleted)}

    def _infer_eval_window_for_query(
        self,
        *,
        code: Optional[str],
        engine_version: str,
        analysis_date_from: Optional[date],
        analysis_date_to: Optional[date],
        analysis_mode: Optional[str] = None,
    ) -> Optional[int]:
        windows = self.repo.get_distinct_eval_windows(
            code=code,
            engine_version=engine_version,
            analysis_date_from=analysis_date_from,
            analysis_date_to=analysis_date_to,
            analysis_mode=analysis_mode,
        )
        return windows[0] if windows else None

    def _get_recent_evaluations_by_phase(
        self,
        *,
        code: Optional[str],
        eval_window_days: Optional[int],
        engine_version: str,
        analysis_mode: Optional[str],
        limit: int,
        page: int,
        analysis_date_from: Optional[date],
        analysis_date_to: Optional[date],
        phase_bucket: str,
    ) -> Dict[str, Any]:
        page_offset = max(page - 1, 0) * limit
        batch_size = max(100, min(500, limit * 4))
        sql_offset = 0
        scanned = 0
        matched_total = 0
        page_rows: List[
            Tuple[
                BacktestResult,
                Optional[str],
                Optional[str],
                Optional[Dict[str, Any]],
                str,
                Optional[str],
                Optional[str],
            ]
        ] = []

        while True:
            remaining_probe_rows = self.MAX_DYNAMIC_SUMMARY_ROWS + 1 - scanned
            if remaining_probe_rows <= 0:
                raise ValueError("Phase-filtered results match too many rows; narrow the analysis date range or stock code.")
            batch_limit = min(batch_size, remaining_probe_rows)
            batch = self.repo.get_results_with_context_batch(
                code=code,
                eval_window_days=eval_window_days,
                engine_version=engine_version,
                analysis_date_from=analysis_date_from,
                analysis_date_to=analysis_date_to,
                days=None,
                analysis_mode=analysis_mode,
                offset=sql_offset,
                limit=batch_limit,
            )
            if not batch:
                break
            scanned += len(batch)
            if scanned > self.MAX_DYNAMIC_SUMMARY_ROWS:
                raise ValueError("Phase-filtered results match too many rows; narrow the analysis date range or stock code.")
            sql_offset += len(batch)
            for (
                result,
                stock_name,
                trend_prediction,
                _created_at,
                context_snapshot,
                raw_result,
                report_type,
            ) in batch:
                summary = extract_market_phase_summary(context_snapshot)
                bucket = self._phase_bucket_from_summary(summary)
                if bucket != phase_bucket:
                    continue
                if matched_total >= page_offset and len(page_rows) < limit:
                    page_rows.append((result, stock_name, trend_prediction, summary, bucket, raw_result, report_type))
                matched_total += 1
            if len(batch) < batch_limit:
                break

        items = [
            self._result_to_dict(
                result,
                stock_name,
                trend_prediction,
                market_phase_summary=summary,
                market_phase=bucket,
                raw_result=raw_result,
                report_type=report_type,
            )
            for result, stock_name, trend_prediction, summary, bucket, raw_result, report_type in page_rows
        ]
        return {"total": matched_total, "page": page, "limit": limit, "items": items}

    @staticmethod
    def _normalize_phase_filter(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = str(value or "").strip().lower()
        if not text or text == "all":
            return None
        allowed = {"premarket", "intraday", "postmarket", "unknown"}
        if text not in allowed:
            raise ValueError("analysis_phase must be one of premarket, intraday, postmarket, unknown")
        return text

    @staticmethod
    def _phase_bucket_from_summary(summary: Optional[Dict[str, Any]]) -> str:
        if not isinstance(summary, dict):
            return "unknown"
        return normalize_analysis_phase_bucket(summary.get("phase"))

    @classmethod
    def _phase_bucket_from_snapshot(cls, context_snapshot: Optional[str]) -> str:
        return cls._phase_bucket_from_summary(extract_market_phase_summary(context_snapshot))

    @classmethod
    def _phase_counts_from_contexts(cls, snapshots: List[Optional[str]]) -> Dict[str, Dict[str, int]]:
        phase_breakdown = {"premarket": 0, "intraday": 0, "postmarket": 0, "unknown": 0}
        raw_phase_counts: Dict[str, int] = {}
        for snapshot in snapshots:
            summary = extract_market_phase_summary(snapshot)
            raw_phase = str(summary.get("phase")) if isinstance(summary, dict) and summary.get("phase") else "unknown"
            raw_phase_counts[raw_phase] = raw_phase_counts.get(raw_phase, 0) + 1
            bucket = cls._phase_bucket_from_summary(summary)
            phase_breakdown[bucket] = phase_breakdown.get(bucket, 0) + 1
        return {"phase_breakdown": phase_breakdown, "raw_phase_counts": raw_phase_counts}

    def _resolve_analysis_date(self, analysis) -> Optional[date]:
        parsed = self.repo.parse_analysis_date_from_snapshot(analysis.context_snapshot)
        if parsed:
            return parsed
        if getattr(analysis, "created_at", None):
            return analysis.created_at.date()
        logger.warning(f"无法确定分析日期，跳过记录: {analysis.code}#{getattr(analysis, 'id', '?')}")
        return None

    @staticmethod
    def _analysis_mode_from_snapshot(analysis) -> str:
        snapshot = parse_json_field(getattr(analysis, "context_snapshot", None))
        if not isinstance(snapshot, dict):
            return "daily"
        mode = snapshot.get("analysis_mode")
        if not mode:
            enhanced_context = snapshot.get("enhanced_context")
            if isinstance(enhanced_context, dict):
                mode = enhanced_context.get("analysis_mode")
        return normalize_analysis_mode(mode)

    def _prepare_evaluation_inputs(
        self,
        *,
        code: str,
        analysis_date: date,
        analysis_created_at: Optional[datetime],
        analysis_mode: str,
        eval_window_days: int,
    ) -> Optional[Dict[str, Any]]:
        if analysis_mode == "hourly":
            return self._prepare_hourly_evaluation_inputs(
                code=code,
                analysis_date=analysis_date,
                analysis_created_at=analysis_created_at,
                eval_window_days=eval_window_days,
            )
        return self._prepare_daily_evaluation_inputs(
            code=code,
            analysis_date=analysis_date,
            eval_window_days=eval_window_days,
        )

    def _prepare_daily_evaluation_inputs(
        self,
        *,
        code: str,
        analysis_date: date,
        eval_window_days: int,
    ) -> Optional[Dict[str, Any]]:
        start_daily = self.stock_repo.get_start_daily(code=code, analysis_date=analysis_date)

        if start_daily is None or start_daily.close is None:
            self._try_fill_daily_data(code=code, analysis_date=analysis_date, eval_window_days=eval_window_days)
            start_daily = self.stock_repo.get_start_daily(code=code, analysis_date=analysis_date)

        if start_daily is None or start_daily.close is None:
            return None

        forward_bars = self.stock_repo.get_forward_bars(
            code=code,
            analysis_date=start_daily.date,
            eval_window_days=eval_window_days,
        )

        if len(forward_bars) < eval_window_days:
            self._try_fill_daily_data(code=code, analysis_date=start_daily.date, eval_window_days=eval_window_days)
            forward_bars = self.stock_repo.get_forward_bars(
                code=code,
                analysis_date=start_daily.date,
                eval_window_days=eval_window_days,
            )

        return {
            "analysis_date": start_daily.date,
            "start_price": start_daily.close,
            "forward_bars": forward_bars,
        }

    def _prepare_hourly_evaluation_inputs(
        self,
        *,
        code: str,
        analysis_date: date,
        analysis_created_at: Optional[datetime],
        eval_window_days: int,
    ) -> Optional[Dict[str, Any]]:
        if not is_crypto_code(code):
            logger.info("普通股票小时线回测缺少小时 K 线数据源，标记为数据不足: %s", code)
            return None

        analysis_at = self._local_naive_to_utc_naive(
            analysis_created_at or datetime.combine(analysis_date, datetime.min.time())
        )
        fetch_days = max(3, (datetime.now() - analysis_at).days + 3)
        try:
            df = CryptoFetcher().get_kline_data(code, period="hourly", days=fetch_days)
        except Exception as exc:
            logger.warning("获取小时线回测 K 线失败(%s): %s", code, exc)
            return None

        bars = self._bars_from_dataframe(df)
        if not bars:
            return None

        window_start = analysis_at
        window_end = analysis_at + timedelta(hours=24 * max(1, int(eval_window_days)))
        candidate_bars = [bar for bar in bars if bar.timestamp > window_start]
        forward_bars = [
            bar
            for bar in candidate_bars
            if bar.timestamp <= window_end
        ][: int(eval_window_days)]
        if not forward_bars:
            return None

        start_bar = max(
            (bar for bar in bars if bar.timestamp <= window_start),
            key=lambda bar: bar.timestamp,
            default=None,
        )
        if start_bar is None or start_bar.close is None:
            start_bar = forward_bars[0]
        if start_bar.close is None:
            return None

        return {
            "analysis_date": start_bar.date,
            "start_price": start_bar.close,
            "forward_bars": forward_bars,
        }

    @staticmethod
    def _bars_from_dataframe(df) -> List[_BacktestBar]:
        if df is None or df.empty:
            return []
        bars: List[_BacktestBar] = []
        for _, row in df.iterrows():
            timestamp = BacktestService._parse_bar_timestamp(row.get("date"))
            high = BacktestService._safe_float(row.get("high"))
            low = BacktestService._safe_float(row.get("low"))
            close = BacktestService._safe_float(row.get("close"))
            if timestamp is None or high is None or low is None or close is None:
                continue
            bars.append(_BacktestBar(timestamp=timestamp, date=timestamp.date(), high=high, low=low, close=close))
        return bars

    @staticmethod
    def _parse_bar_timestamp(value: Any) -> Optional[datetime]:
        text = str(value or "").strip()
        if not text:
            return None
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(text[:16] if "%H" in fmt else text[:10], fmt)
            except ValueError:
                continue
        return None

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed == parsed else None

    @staticmethod
    def _local_naive_to_utc_naive(value: datetime) -> datetime:
        if value.tzinfo is not None and value.utcoffset() is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        local_tz = datetime.now().astimezone().tzinfo
        if local_tz is None:
            return value
        return value.replace(tzinfo=local_tz).astimezone(timezone.utc).replace(tzinfo=None)

    def _try_fill_daily_data(self, *, code: str, analysis_date: date, eval_window_days: int) -> None:
        try:
            from data_provider.base import DataFetcherManager

            # fetch a window that covers start + forward bars
            end_date = analysis_date + timedelta(days=max(eval_window_days * 2, 30))
            manager = DataFetcherManager()
            df, source = manager.get_daily_data(
                stock_code=code,
                start_date=analysis_date.strftime("%Y-%m-%d"),
                end_date=end_date.strftime("%Y-%m-%d"),
                days=eval_window_days * 2,
            )
            if df is None or df.empty:
                return
            self.db.save_daily_data(df, code=code, data_source=source)
        except Exception as exc:
            logger.warning(f"补全日线数据失败({code}): {exc}")

    def _recompute_summaries(
        self,
        *,
        touched_codes: List[str],
        eval_window_days: int,
        engine_version: str,
        analysis_mode: Optional[str] = None,
    ) -> None:
        normalized_mode = normalize_analysis_mode(analysis_mode) if analysis_mode else "daily"
        persist_summary = normalized_mode == "daily"
        with self.db.get_session() as session:
            # overall
            overall_rows = session.execute(
                select(BacktestResult).where(
                    and_(
                        BacktestResult.eval_window_days == eval_window_days,
                        BacktestResult.engine_version == engine_version,
                        BacktestResult.analysis_mode == normalized_mode,
                    )
                )
            ).scalars().all()
            overall_data = BacktestEngine.compute_summary(
                results=overall_rows,
                scope="overall",
                code=OVERALL_SENTINEL_CODE,
                eval_window_days=eval_window_days,
                engine_version=engine_version,
            )
            overall_data["analysis_mode"] = normalized_mode
            if persist_summary:
                overall_summary = self._build_summary_model(overall_data)
                self.repo.upsert_summary(overall_summary)

            for code in touched_codes:
                rows = session.execute(
                    select(BacktestResult).where(
                        and_(
                            BacktestResult.code == code,
                            BacktestResult.eval_window_days == eval_window_days,
                            BacktestResult.engine_version == engine_version,
                            BacktestResult.analysis_mode == normalized_mode,
                        )
                    )
                ).scalars().all()
                data = BacktestEngine.compute_summary(
                    results=rows,
                    scope="stock",
                    code=code,
                    eval_window_days=eval_window_days,
                    engine_version=engine_version,
                )
                data["analysis_mode"] = normalized_mode
                if persist_summary:
                    summary = self._build_summary_model(data)
                    self.repo.upsert_summary(summary)

    @staticmethod
    def _build_summary_model(summary_data: Dict[str, Any]) -> BacktestSummary:
        return BacktestSummary(
            scope=summary_data.get("scope"),
            code=summary_data.get("code"),
            eval_window_days=summary_data.get("eval_window_days"),
            engine_version=summary_data.get("engine_version"),
            analysis_mode=summary_data.get("analysis_mode") or "daily",
            computed_at=datetime.now(),
            total_evaluations=summary_data.get("total_evaluations") or 0,
            completed_count=summary_data.get("completed_count") or 0,
            insufficient_count=summary_data.get("insufficient_count") or 0,
            long_count=summary_data.get("long_count") or 0,
            cash_count=summary_data.get("cash_count") or 0,
            win_count=summary_data.get("win_count") or 0,
            loss_count=summary_data.get("loss_count") or 0,
            neutral_count=summary_data.get("neutral_count") or 0,
            direction_accuracy_pct=summary_data.get("direction_accuracy_pct"),
            win_rate_pct=summary_data.get("win_rate_pct"),
            neutral_rate_pct=summary_data.get("neutral_rate_pct"),
            avg_stock_return_pct=summary_data.get("avg_stock_return_pct"),
            avg_simulated_return_pct=summary_data.get("avg_simulated_return_pct"),
            stop_loss_trigger_rate=summary_data.get("stop_loss_trigger_rate"),
            take_profit_trigger_rate=summary_data.get("take_profit_trigger_rate"),
            ambiguous_rate=summary_data.get("ambiguous_rate"),
            avg_days_to_first_hit=summary_data.get("avg_days_to_first_hit"),
            advice_breakdown_json=json.dumps(summary_data.get("advice_breakdown") or {}, ensure_ascii=False),
            diagnostics_json=json.dumps(summary_data.get("diagnostics") or {}, ensure_ascii=False),
        )

    @staticmethod
    def _result_to_dict(
        row: BacktestResult,
        stock_name: Optional[str] = None,
        trend_prediction: Optional[str] = None,
        market_phase_summary: Optional[Dict[str, Any]] = None,
        market_phase: Optional[str] = None,
        raw_result: Optional[Any] = None,
        report_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        parsed_raw_result = parse_json_field(raw_result)
        raw = parsed_raw_result if isinstance(parsed_raw_result, dict) else {}
        action_fields = build_action_fields(
            operation_advice=raw.get("operation_advice") or row.operation_advice,
            explicit_action=raw.get("action"),
            report_type=report_type or ("market_review" if row.code == "market_review" else None),
            report_language=raw.get("report_language"),
        )
        return {
            "analysis_history_id": row.analysis_history_id,
            "code": row.code,
            "stock_name": stock_name,
            "analysis_date": row.analysis_date.isoformat() if row.analysis_date else None,
            "analysis_mode": row.analysis_mode or "daily",
            "analysis_timeframe": analysis_timeframe_label(row.analysis_mode or "daily", raw.get("report_language") or "zh"),
            "eval_window_days": row.eval_window_days,
            "engine_version": row.engine_version,
            "eval_status": row.eval_status,
            "evaluated_at": row.evaluated_at.isoformat() if row.evaluated_at else None,
            "operation_advice": row.operation_advice,
            "action": action_fields["action"],
            "action_label": action_fields["action_label"],
            "trend_prediction": trend_prediction,
            "market_phase": market_phase,
            "market_phase_summary": market_phase_summary,
            "position_recommendation": row.position_recommendation,
            "start_price": row.start_price,
            "end_close": row.end_close,
            "max_high": row.max_high,
            "min_low": row.min_low,
            "stock_return_pct": row.stock_return_pct,
            "actual_return_pct": row.stock_return_pct,
            "actual_movement": BacktestService._actual_movement_from_return(row.stock_return_pct),
            "direction_expected": row.direction_expected,
            "direction_correct": row.direction_correct,
            "outcome": row.outcome,
            "stop_loss": row.stop_loss,
            "take_profit": row.take_profit,
            "hit_stop_loss": row.hit_stop_loss,
            "hit_take_profit": row.hit_take_profit,
            "first_hit": row.first_hit,
            "first_hit_date": row.first_hit_date.isoformat() if row.first_hit_date else None,
            "first_hit_trading_days": row.first_hit_trading_days,
            "simulated_entry_price": row.simulated_entry_price,
            "simulated_exit_price": row.simulated_exit_price,
            "simulated_exit_reason": row.simulated_exit_reason,
            "simulated_return_pct": row.simulated_return_pct,
        }

    @staticmethod
    def _summary_to_dict(row: BacktestSummary) -> Dict[str, Any]:
        return {
            "scope": row.scope,
            "code": None if row.code == OVERALL_SENTINEL_CODE else row.code,
            "eval_window_days": row.eval_window_days,
            "engine_version": row.engine_version,
            "analysis_mode": row.analysis_mode or "daily",
            "analysis_timeframe": analysis_timeframe_label(row.analysis_mode or "daily"),
            "computed_at": row.computed_at.isoformat() if row.computed_at else None,
            "total_evaluations": row.total_evaluations,
            "completed_count": row.completed_count,
            "insufficient_count": row.insufficient_count,
            "long_count": row.long_count,
            "cash_count": row.cash_count,
            "win_count": row.win_count,
            "loss_count": row.loss_count,
            "neutral_count": row.neutral_count,
            "direction_accuracy_pct": row.direction_accuracy_pct,
            "win_rate_pct": row.win_rate_pct,
            "neutral_rate_pct": row.neutral_rate_pct,
            "avg_stock_return_pct": row.avg_stock_return_pct,
            "avg_simulated_return_pct": row.avg_simulated_return_pct,
            "stop_loss_trigger_rate": row.stop_loss_trigger_rate,
            "take_profit_trigger_rate": row.take_profit_trigger_rate,
            "ambiguous_rate": row.ambiguous_rate,
            "avg_days_to_first_hit": row.avg_days_to_first_hit,
            "advice_breakdown": json.loads(row.advice_breakdown_json) if row.advice_breakdown_json else {},
            "diagnostics": json.loads(row.diagnostics_json) if row.diagnostics_json else {},
        }

    @staticmethod
    def _normalize_learning_summary(summary: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Normalize summary metrics to the ratio-based shape expected by Agent memory."""
        if summary is None:
            return None

        normalized = dict(summary)
        normalized["win_rate"] = BacktestService._pct_to_ratio(summary.get("win_rate_pct"), default=0.5)
        normalized["direction_accuracy"] = BacktestService._pct_to_ratio(
            summary.get("direction_accuracy_pct"),
            default=0.5,
        )

        avg_return_pct = summary.get("avg_simulated_return_pct")
        if avg_return_pct is None:
            avg_return_pct = summary.get("avg_stock_return_pct")
        normalized["avg_return"] = BacktestService._pct_to_ratio(avg_return_pct, default=0.0)
        return normalized

    @staticmethod
    def _pct_to_ratio(value: Optional[float], default: float = 0.0) -> float:
        try:
            return float(value) / 100.0
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _actual_movement_from_return(value: Optional[float]) -> Optional[str]:
        if value is None:
            return None
        try:
            actual_return = float(value)
        except (TypeError, ValueError):
            return None
        if actual_return > 0:
            return "up"
        if actual_return < 0:
            return "down"
        return "flat"

    @staticmethod
    def _build_dynamic_summary(
        *,
        rows: List[BacktestResult],
        scope: str,
        code: Optional[str],
        eval_window_days: Optional[int],
        engine_version: str,
        analysis_mode: Optional[str] = None,
        max_rows: Optional[int] = None,
        phase_breakdown: Optional[Dict[str, int]] = None,
        raw_phase_counts: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        filtered_rows = [row for row in rows if getattr(row, "engine_version", None) == engine_version]
        if eval_window_days is not None:
            summary_window_days = int(eval_window_days)
        else:
            window_values = sorted({
                int(row.eval_window_days)
                for row in filtered_rows
                if getattr(row, "eval_window_days", None) is not None
            })
            if len(window_values) > 1:
                logger.warning(
                    "Multiple eval_window_days values found for dynamic summary; using %s for engine_version=%s, scope=%s, code=%s",
                    window_values[0],
                    engine_version,
                    scope,
                    code,
                )
            if window_values:
                summary_window_days = window_values[0]
            else:
                summary_window_days = int(getattr(get_config(), "backtest_eval_window_days", 10))

        filtered_rows = [
            row for row in filtered_rows if getattr(row, "eval_window_days", None) == summary_window_days
        ]
        normalized_mode = normalize_analysis_mode(analysis_mode) if analysis_mode else None
        if normalized_mode is not None:
            filtered_rows = [
                row for row in filtered_rows if getattr(row, "analysis_mode", "daily") == normalized_mode
            ]

        if max_rows is not None and len(filtered_rows) > max_rows:
            raise ValueError(
                "Date-filtered summary matches too many rows; narrow the analysis date range or stock code."
            )

        summary = BacktestEngine.compute_summary(
            results=filtered_rows,
            scope=scope,
            code=code,
            eval_window_days=summary_window_days,
            engine_version=engine_version,
        )
        diagnostics = summary.get("diagnostics")
        if not isinstance(diagnostics, dict):
            diagnostics = {}
        if phase_breakdown is not None:
            diagnostics["phase_breakdown"] = phase_breakdown
        if raw_phase_counts is not None:
            diagnostics["raw_phase_counts"] = raw_phase_counts
        summary["diagnostics"] = diagnostics
        summary["analysis_mode"] = normalized_mode
        summary["analysis_timeframe"] = analysis_timeframe_label(normalized_mode) if normalized_mode else None
        summary["code"] = None if summary.get("code") == OVERALL_SENTINEL_CODE else summary.get("code")
        summary["computed_at"] = datetime.now().isoformat()
        return summary
