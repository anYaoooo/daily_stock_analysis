# -*- coding: utf-8 -*-
"""Repository helpers for BTC plan-level backtests."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import and_, delete, desc, func, or_, select

from data_provider.crypto_fetcher import is_crypto_code, normalize_crypto_symbol
from src.core.btc_only import BTC_CANONICAL_CODE
from src.storage import (
    AnalysisHistory,
    CryptoBacktestResult,
    CryptoBacktestSummary,
    DatabaseManager,
)

logger = logging.getLogger(__name__)


class CryptoBacktestRepository:
    """DB access layer for BTC plan backtesting."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    def get_candidates(
        self,
        *,
        code: Optional[str],
        min_age_hours: int,
        limit: int,
        engine_version: str,
        force: bool,
    ) -> list[AnalysisHistory]:
        cutoff_dt = datetime.now() - timedelta(hours=max(0, int(min_age_hours)))

        with self.db.get_session() as session:
            conditions = [
                AnalysisHistory.created_at <= cutoff_dt,
                or_(
                    AnalysisHistory.report_type.is_(None),
                    AnalysisHistory.report_type != "market_review",
                ),
            ]
            if code:
                code_variants = {
                    str(code).strip().upper(),
                    BTC_CANONICAL_CODE,
                }
                normalized_symbol = normalize_crypto_symbol(code)
                if normalized_symbol:
                    code_variants.add(normalized_symbol)
                conditions.append(AnalysisHistory.code.in_(sorted(code_variants)))

            query = select(AnalysisHistory).where(and_(*conditions))
            if not force:
                provisional_ids = select(CryptoBacktestResult.analysis_history_id).where(
                    and_(
                        CryptoBacktestResult.engine_version == engine_version,
                        CryptoBacktestResult.eval_status.in_({"pending", "insufficient_data"}),
                    )
                )
                terminal_only_ids = select(CryptoBacktestResult.analysis_history_id).where(
                    and_(
                        CryptoBacktestResult.engine_version == engine_version,
                        CryptoBacktestResult.analysis_history_id.not_in(provisional_ids),
                    )
                )
                query = query.where(AnalysisHistory.id.not_in(terminal_only_ids))

            rows = session.execute(
                query.order_by(desc(AnalysisHistory.created_at)).limit(int(limit))
            ).scalars().all()
            return [row for row in rows if is_crypto_code(row.code)]

    def get_history_records(
        self,
        *,
        ids: Optional[list[int]] = None,
        code: Optional[str] = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[AnalysisHistory], int]:
        with self.db.get_session() as session:
            btc_code_variants = {
                "BTC",
                BTC_CANONICAL_CODE,
                "BTCUSDT",
                "BTCUSD",
                "BTC-USD",
                "BTC/USD",
                "BTC_USDT",
            }
            conditions = [
                AnalysisHistory.code.in_(sorted(btc_code_variants)),
                or_(
                    AnalysisHistory.report_type.is_(None),
                    AnalysisHistory.report_type != "market_review",
                ),
            ]
            if ids:
                conditions.append(AnalysisHistory.id.in_([int(item) for item in ids]))
            if code:
                code_variants = {
                    str(code).strip().upper(),
                    BTC_CANONICAL_CODE,
                }
                normalized_symbol = normalize_crypto_symbol(code)
                if normalized_symbol:
                    code_variants.add(normalized_symbol)
                conditions.append(AnalysisHistory.code.in_(sorted(code_variants)))

            where_clause = and_(*conditions)
            total = session.execute(
                select(func.count(AnalysisHistory.id))
                .select_from(AnalysisHistory)
                .where(where_clause)
            ).scalar() or 0
            rows = session.execute(
                select(AnalysisHistory)
                .where(where_clause)
                .order_by(desc(AnalysisHistory.created_at), desc(AnalysisHistory.id))
                .offset(max(0, int(offset)))
                .limit(int(limit))
            ).scalars().all()
            return [row for row in rows if is_crypto_code(row.code)], int(total)

    def get_results_for_history_ids(
        self,
        *,
        analysis_history_ids: list[int],
        engine_version: str,
    ) -> list[CryptoBacktestResult]:
        ids = sorted({int(item) for item in analysis_history_ids if item is not None})
        if not ids:
            return []
        with self.db.get_session() as session:
            rows = session.execute(
                select(CryptoBacktestResult)
                .where(
                    and_(
                        CryptoBacktestResult.analysis_history_id.in_(ids),
                        CryptoBacktestResult.engine_version == engine_version,
                    )
                )
                .order_by(desc(CryptoBacktestResult.evaluated_at), desc(CryptoBacktestResult.id))
            ).scalars().all()
            return list(rows)

    def save_results_batch(
        self,
        results: list[CryptoBacktestResult],
        *,
        replace_existing: bool = False,
    ) -> int:
        if not results:
            return 0

        with self.db.get_session() as session:
            try:
                saved = 0
                for row in results:
                    existing = session.execute(
                        select(CryptoBacktestResult).where(
                            and_(
                                CryptoBacktestResult.analysis_history_id == row.analysis_history_id,
                                CryptoBacktestResult.plan_type == row.plan_type,
                                CryptoBacktestResult.engine_version == row.engine_version,
                            )
                        )
                    ).scalar_one_or_none()
                    if existing is not None:
                        if not replace_existing and existing.eval_status not in {"pending", "insufficient_data"}:
                            continue
                        session.delete(existing)
                        session.flush()
                    session.add(row)
                    saved += 1
                session.commit()
                return saved
            except Exception as exc:
                session.rollback()
                logger.error("批量保存 BTC 回测结果失败: %s", exc)
                raise

    def delete_result(
        self,
        *,
        analysis_history_id: int,
        plan_type: str,
        engine_version: str,
    ) -> tuple[int, Optional[str]]:
        with self.db.get_session() as session:
            row = session.execute(
                select(CryptoBacktestResult).where(
                    and_(
                        CryptoBacktestResult.analysis_history_id == int(analysis_history_id),
                        CryptoBacktestResult.plan_type == plan_type,
                        CryptoBacktestResult.engine_version == engine_version,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return 0, None
            code = row.code
            session.delete(row)
            session.commit()
            return 1, code

    def list_results(
        self,
        *,
        code: Optional[str] = None,
        horizon: Optional[str] = None,
        plan_type: Optional[str] = None,
        engine_version: Optional[str] = None,
        net_loss_only: bool = False,
        limit: Optional[int] = None,
    ) -> list[CryptoBacktestResult]:
        with self.db.get_session() as session:
            conditions = self._build_result_conditions(
                code=code,
                horizon=horizon,
                plan_type=plan_type,
                engine_version=engine_version,
            )
            if net_loss_only:
                conditions.extend(
                    [
                        CryptoBacktestResult.eval_status == "completed",
                        CryptoBacktestResult.entry_triggered.is_(True),
                        or_(
                            CryptoBacktestResult.outcome == "loss",
                            CryptoBacktestResult.simulated_return_pct < 0,
                        ),
                    ]
                )
            query = (
                select(CryptoBacktestResult)
                .where(and_(*conditions) if conditions else True)
                .order_by(desc(CryptoBacktestResult.analysis_created_at), desc(CryptoBacktestResult.evaluated_at))
            )
            if limit is not None:
                query = query.limit(int(limit))
            return list(session.execute(query).scalars().all())

    def get_results_paginated(
        self,
        *,
        code: Optional[str],
        horizon: Optional[str],
        plan_type: Optional[str],
        direction: Optional[str],
        result_status: Optional[str],
        engine_version: Optional[str],
        offset: int,
        limit: int,
    ) -> tuple[list[CryptoBacktestResult], int]:
        with self.db.get_session() as session:
            conditions = self._build_result_conditions(
                code=code,
                horizon=horizon,
                plan_type=plan_type,
                direction=direction,
                result_status=result_status,
                engine_version=engine_version,
            )
            where_clause = and_(*conditions) if conditions else True
            total = session.execute(
                select(func.count(CryptoBacktestResult.id))
                .select_from(CryptoBacktestResult)
                .where(where_clause)
            ).scalar() or 0
            rows = session.execute(
                select(CryptoBacktestResult)
                .where(where_clause)
                .order_by(desc(CryptoBacktestResult.analysis_created_at), desc(CryptoBacktestResult.evaluated_at))
                .offset(max(0, int(offset)))
                .limit(int(limit))
            ).scalars().all()
            return list(rows), int(total)

    def upsert_summary(self, summary: CryptoBacktestSummary) -> None:
        with self.db.get_session() as session:
            existing = session.execute(
                select(CryptoBacktestSummary)
                .where(
                    and_(
                        CryptoBacktestSummary.scope == summary.scope,
                        CryptoBacktestSummary.code == summary.code,
                        CryptoBacktestSummary.horizon == summary.horizon,
                        CryptoBacktestSummary.plan_type == summary.plan_type,
                        CryptoBacktestSummary.engine_version == summary.engine_version,
                    )
                )
                .limit(1)
            ).scalar_one_or_none()

            if existing:
                for attr in (
                    "computed_at",
                    "total_evaluations",
                    "completed_count",
                    "triggered_count",
                    "no_entry_count",
                    "skipped_count",
                    "insufficient_count",
                    "win_count",
                    "loss_count",
                    "neutral_count",
                    "direction_accuracy_pct",
                    "direction_accuracy_raw_pct",
                    "signal_quality_rate_pct",
                    "execution_fill_rate_pct",
                    "win_rate_pct",
                    "avg_simulated_return_pct",
                    "plan_type_breakdown_json",
                    "diagnostics_json",
                ):
                    setattr(existing, attr, getattr(summary, attr))
                session.commit()
                return

            session.add(summary)
            session.commit()

    def delete_summaries(self, *, engine_version: str) -> int:
        """Delete derived summaries before rebuilding a complete engine snapshot."""
        with self.db.get_session() as session:
            result = session.execute(
                delete(CryptoBacktestSummary).where(
                    CryptoBacktestSummary.engine_version == engine_version
                )
            )
            session.commit()
            return int(result.rowcount or 0)

    def get_summary(
        self,
        *,
        scope: str,
        code: Optional[str],
        horizon: Optional[str],
        plan_type: Optional[str],
        engine_version: str,
    ) -> Optional[CryptoBacktestSummary]:
        with self.db.get_session() as session:
            row = session.execute(
                select(CryptoBacktestSummary)
                .where(
                    and_(
                        CryptoBacktestSummary.scope == scope,
                        CryptoBacktestSummary.code == code,
                        CryptoBacktestSummary.horizon == horizon,
                        CryptoBacktestSummary.plan_type == plan_type,
                        CryptoBacktestSummary.engine_version == engine_version,
                    )
                )
                .order_by(desc(CryptoBacktestSummary.computed_at))
                .limit(1)
            ).scalar_one_or_none()
            return row

    @staticmethod
    def _build_result_conditions(
        *,
        code: Optional[str],
        horizon: Optional[str],
        plan_type: Optional[str],
        direction: Optional[str] = None,
        result_status: Optional[str] = None,
        engine_version: Optional[str] = None,
    ) -> list[object]:
        conditions = []
        if code:
            conditions.append(CryptoBacktestResult.code == code)
        if horizon:
            conditions.append(CryptoBacktestResult.horizon == horizon)
        if plan_type:
            conditions.append(CryptoBacktestResult.plan_type == plan_type)
        if direction:
            conditions.append(CryptoBacktestResult.direction == direction)
        if result_status:
            if result_status in {"win", "loss", "neutral", "no_entry", "skipped", "insufficient_data"}:
                conditions.append(CryptoBacktestResult.outcome == result_status)
            else:
                conditions.append(CryptoBacktestResult.eval_status == result_status)
        if engine_version:
            conditions.append(CryptoBacktestResult.engine_version == engine_version)
        return conditions
