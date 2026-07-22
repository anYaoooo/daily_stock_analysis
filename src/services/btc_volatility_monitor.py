# -*- coding: utf-8 -*-
"""BTC price-volatility trigger for event-driven hourly analysis."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from data_provider.crypto_fetcher import CryptoFetcher

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PriceSnapshot:
    timestamp: float
    price: float
    provider_timestamp: Optional[str] = None


@dataclass
class ActiveOpportunity:
    direction: str
    detected_at: float
    baseline_timestamp: float
    baseline_price: float
    opportunity_price: float
    threshold_pct: float
    initial_change_pct: float
    provider_timestamp: Optional[str] = None


class BTCVolatilityMonitor:
    """Track BTC quotes and trigger hourly analysis only after an entry signal forms."""

    def __init__(
        self,
        *,
        quote_fetcher: Optional[Callable[[str], Any]] = None,
        now_provider: Optional[Callable[[], float]] = None,
    ) -> None:
        self._rest_fetcher = CryptoFetcher()
        self.quote_fetcher = quote_fetcher or self._rest_fetcher.get_realtime_quote
        self.now_provider = now_provider or time.time
        self._snapshots: List[PriceSnapshot] = []
        self._last_trigger_at: Optional[float] = None
        self._confirmation_direction: Optional[str] = None
        self._confirmation_count = 0
        self._early_warning_direction: Optional[str] = None
        self._active_opportunity: Optional[ActiveOpportunity] = None
        self._last_quote_error_log_at: Optional[float] = None

    def run_once(self, config: Any) -> Dict[str, Any]:
        """Check one quote and return trigger metadata for the scheduler."""
        stats: Dict[str, Any] = {
            "checked": 0,
            "triggered": 0,
            "event_detected": 0,
            "suppressed": 0,
            "reason": "",
        }
        if not getattr(config, "btc_volatility_monitor_enabled", False):
            stats["reason"] = "disabled"
            return stats

        symbol = str(getattr(config, "btc_volatility_monitor_symbol", "BTC") or "BTC").strip() or "BTC"
        now = float(self.now_provider())
        try:
            quote = self.quote_fetcher(symbol)
        except Exception as exc:
            if self._last_quote_error_log_at is None or now - self._last_quote_error_log_at >= 60:
                logger.warning("BTC 行情暂不可用，波动监控本轮跳过: %s", exc)
                self._last_quote_error_log_at = now
            stats["reason"] = "quote_error"
            stats["error_type"] = type(exc).__name__
            return stats
        price = self._quote_price(quote)
        if price is None or price <= 0:
            stats["reason"] = "missing_price"
            return stats

        stats["checked"] = 1
        window_seconds = max(
            30,
            int(getattr(config, "btc_volatility_monitor_window_minutes", 5) or 5) * 60,
        )
        threshold_pct = max(
            0.1,
            float(getattr(config, "btc_volatility_monitor_threshold_pct", 1.0) or 1.0),
        )
        early_warning_pct = min(
            threshold_pct,
            max(
                0.1,
                float(getattr(config, "btc_volatility_monitor_early_warning_pct", 0.3) or 0.3),
            ),
        )
        confirmation_samples = max(
            1,
            int(getattr(config, "btc_volatility_monitor_confirmation_samples", 2) or 2),
        )
        entry_confirmation_pct = max(
            0.0,
            float(getattr(config, "btc_volatility_monitor_entry_confirmation_pct", 0.2) or 0.0),
        )
        invalidation_pct = max(
            0.1,
            float(getattr(config, "btc_volatility_monitor_invalidation_pct", 0.5) or 0.5),
        )
        max_watch_seconds = max(
            60,
            int(getattr(config, "btc_volatility_monitor_max_watch_minutes", 20) or 20) * 60,
        )
        cooldown_seconds = max(
            0,
            int(getattr(config, "btc_volatility_monitor_cooldown_minutes", 30) or 30) * 60,
        )

        snapshot = PriceSnapshot(
            timestamp=now,
            price=float(price),
            provider_timestamp=self._quote_provider_timestamp(quote),
        )
        self._snapshots.append(snapshot)
        self._prune(now=now, window_seconds=window_seconds)

        active_stats = self._evaluate_active_opportunity(
            snapshot=snapshot,
            now=now,
            confirmation_samples=confirmation_samples,
            entry_confirmation_pct=entry_confirmation_pct,
            invalidation_pct=invalidation_pct,
            max_watch_seconds=max_watch_seconds,
            cooldown_seconds=cooldown_seconds,
        )
        if active_stats is not None:
            stats.update(active_stats)
            return stats

        if len(self._snapshots) < 2:
            stats["reason"] = "warming_up"
            stats.update(self._snapshot_fields(snapshot))
            return stats

        baseline = self._snapshots[0]
        change_pct = (snapshot.price - baseline.price) / baseline.price * 100
        direction = "up" if change_pct > 0 else "down"
        if abs(change_pct) < early_warning_pct:
            self._early_warning_direction = None
            self._reset_confirmation()
            stats["reason"] = "below_threshold"
            stats.update(
                self._market_fields(
                    snapshot=snapshot,
                    baseline=baseline,
                    change_pct=change_pct,
                    threshold_pct=threshold_pct,
                )
            )
            return stats

        if abs(change_pct) < threshold_pct:
            stats["reason"] = "early_warning_active"
            if self._early_warning_direction != direction:
                self._early_warning_direction = direction
                stats["early_warning_detected"] = 1
                stats["reason"] = "early_warning"
                stats["trigger_reason"] = "early_warning"
            stats.update(
                self._early_warning_fields(
                    snapshot=snapshot,
                    baseline=baseline,
                    change_pct=change_pct,
                    threshold_pct=threshold_pct,
                    early_warning_pct=early_warning_pct,
                    entry_confirmation_pct=entry_confirmation_pct,
                    invalidation_pct=invalidation_pct,
                )
            )
            return stats

        self._early_warning_direction = None
        self._active_opportunity = ActiveOpportunity(
            direction=direction,
            detected_at=now,
            baseline_timestamp=baseline.timestamp,
            baseline_price=baseline.price,
            opportunity_price=snapshot.price,
            threshold_pct=threshold_pct,
            initial_change_pct=change_pct,
            provider_timestamp=snapshot.provider_timestamp,
        )
        self._confirmation_direction = direction
        self._confirmation_count = 1

        stats["reason"] = "awaiting_confirmation"
        # Reaching the threshold is itself user-visible market information. The
        # scheduler sends this once immediately; a later confirmed entry still
        # triggers the full hourly analysis below.
        stats["event_detected"] = 1
        stats["trigger_reason"] = "volatility_spike"
        stats.update(
            self._opportunity_fields(
                snapshot=snapshot,
                opportunity=self._active_opportunity,
                confirmation_count=self._confirmation_count,
                confirmation_required=confirmation_samples,
                entry_confirmation_pct=entry_confirmation_pct,
                invalidation_pct=invalidation_pct,
            )
        )
        return stats

    def _reset_confirmation(self) -> None:
        self._confirmation_direction = None
        self._confirmation_count = 0
        self._active_opportunity = None

    @classmethod
    def _early_warning_fields(
        cls,
        *,
        snapshot: PriceSnapshot,
        baseline: PriceSnapshot,
        change_pct: float,
        threshold_pct: float,
        early_warning_pct: float,
        entry_confirmation_pct: float,
        invalidation_pct: float,
    ) -> Dict[str, Any]:
        """Expose the next confirmation levels before the full move completes."""
        direction = "up" if change_pct > 0 else "down"
        if direction == "up":
            threshold_price = baseline.price * (1 + threshold_pct / 100)
            entry_price = threshold_price * (1 + entry_confirmation_pct / 100)
            invalidation_price = threshold_price * (1 - invalidation_pct / 100)
            trade_direction = "long"
        else:
            threshold_price = baseline.price * (1 - threshold_pct / 100)
            entry_price = threshold_price * (1 - entry_confirmation_pct / 100)
            invalidation_price = threshold_price * (1 + invalidation_pct / 100)
            trade_direction = "short"

        fields = cls._market_fields(
            snapshot=snapshot,
            baseline=baseline,
            change_pct=change_pct,
            threshold_pct=threshold_pct,
        )
        fields.update(
            {
                "opportunity_state": "early_warning",
                "early_warning_threshold_pct": round(early_warning_pct, 4),
                "trade_direction": trade_direction,
                "suggested_trade_action": f"watch_{trade_direction}_confirmation",
                "threshold_price": round(threshold_price, 4),
                "entry_confirmation_pct": round(entry_confirmation_pct, 4),
                "entry_price": round(entry_price, 4),
                "invalidation_pct": round(invalidation_pct, 4),
                "invalidation_price": round(invalidation_price, 4),
            }
        )
        return fields

    def _evaluate_active_opportunity(
        self,
        *,
        snapshot: PriceSnapshot,
        now: float,
        confirmation_samples: int,
        entry_confirmation_pct: float,
        invalidation_pct: float,
        max_watch_seconds: int,
        cooldown_seconds: int,
    ) -> Optional[Dict[str, Any]]:
        opportunity = self._active_opportunity
        if opportunity is None:
            return None

        watched_seconds = max(0, int(now - opportunity.detected_at))
        if watched_seconds > max_watch_seconds:
            fields = self._opportunity_fields(
                snapshot=snapshot,
                opportunity=opportunity,
                confirmation_count=self._confirmation_count,
                confirmation_required=confirmation_samples,
                entry_confirmation_pct=entry_confirmation_pct,
                invalidation_pct=invalidation_pct,
            )
            fields["reason"] = "watch_expired"
            self._reset_confirmation()
            return fields

        if self._is_opportunity_invalidated(snapshot, opportunity, invalidation_pct):
            fields = self._opportunity_fields(
                snapshot=snapshot,
                opportunity=opportunity,
                confirmation_count=self._confirmation_count,
                confirmation_required=confirmation_samples,
                entry_confirmation_pct=entry_confirmation_pct,
                invalidation_pct=invalidation_pct,
            )
            fields["reason"] = "opportunity_invalidated"
            self._reset_confirmation()
            return fields

        if self._entry_signal_confirmed(snapshot, opportunity, entry_confirmation_pct):
            if self._confirmation_direction == opportunity.direction:
                self._confirmation_count += 1
            else:
                self._confirmation_direction = opportunity.direction
                self._confirmation_count = 1
        else:
            self._confirmation_count = max(1, self._confirmation_count)

        fields = self._opportunity_fields(
            snapshot=snapshot,
            opportunity=opportunity,
            confirmation_count=self._confirmation_count,
            confirmation_required=confirmation_samples,
            entry_confirmation_pct=entry_confirmation_pct,
            invalidation_pct=invalidation_pct,
        )
        if self._confirmation_count < confirmation_samples:
            fields["reason"] = "watching_opportunity"
            return fields

        if self._last_trigger_at is not None and now - self._last_trigger_at < cooldown_seconds:
            fields["suppressed"] = 1
            fields["reason"] = "cooldown"
            return fields

        self._last_trigger_at = now
        fields["triggered"] = 1
        fields["reason"] = "entry_signal"
        fields["trigger_reason"] = "entry_signal"
        self._reset_confirmation()
        return fields

    @staticmethod
    def _entry_signal_confirmed(
        snapshot: PriceSnapshot,
        opportunity: ActiveOpportunity,
        entry_confirmation_pct: float,
    ) -> bool:
        if opportunity.direction == "up":
            required_price = opportunity.opportunity_price * (1 + entry_confirmation_pct / 100)
            return snapshot.price >= required_price
        required_price = opportunity.opportunity_price * (1 - entry_confirmation_pct / 100)
        return snapshot.price <= required_price

    @staticmethod
    def _is_opportunity_invalidated(
        snapshot: PriceSnapshot,
        opportunity: ActiveOpportunity,
        invalidation_pct: float,
    ) -> bool:
        if opportunity.direction == "up":
            invalidation_price = opportunity.opportunity_price * (1 - invalidation_pct / 100)
            return snapshot.price <= invalidation_price
        invalidation_price = opportunity.opportunity_price * (1 + invalidation_pct / 100)
        return snapshot.price >= invalidation_price

    def _prune(self, *, now: float, window_seconds: int) -> None:
        cutoff = now - window_seconds
        self._snapshots = [snapshot for snapshot in self._snapshots if snapshot.timestamp >= cutoff]

    @staticmethod
    def _quote_price(quote: Any) -> Optional[float]:
        if quote is None:
            return None
        raw = quote.get("price") if isinstance(quote, dict) else getattr(quote, "price", None)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _quote_provider_timestamp(quote: Any) -> Optional[str]:
        if quote is None:
            return None
        raw = (
            quote.get("provider_timestamp")
            if isinstance(quote, dict)
            else getattr(quote, "provider_timestamp", None)
        )
        return str(raw) if raw else None

    @staticmethod
    def _snapshot_fields(snapshot: PriceSnapshot) -> Dict[str, Any]:
        return {
            "price": round(snapshot.price, 4),
            "provider_timestamp": snapshot.provider_timestamp,
        }

    @classmethod
    def _market_fields(
        cls,
        *,
        snapshot: PriceSnapshot,
        baseline: PriceSnapshot,
        change_pct: float,
        threshold_pct: float,
        confirmation_count: Optional[int] = None,
        confirmation_required: Optional[int] = None,
    ) -> Dict[str, Any]:
        fields = cls._snapshot_fields(snapshot)
        fields.update(
            {
                "baseline_price": round(baseline.price, 4),
                "change_pct": round(change_pct, 4),
                "threshold_pct": round(threshold_pct, 4),
                "direction": "up" if change_pct > 0 else "down",
                "window_seconds": int(snapshot.timestamp - baseline.timestamp),
            }
        )
        if confirmation_count is not None:
            fields["confirmation_count"] = int(confirmation_count)
        if confirmation_required is not None:
            fields["confirmation_required"] = int(confirmation_required)
        return fields

    @classmethod
    def _opportunity_fields(
        cls,
        *,
        snapshot: PriceSnapshot,
        opportunity: ActiveOpportunity,
        confirmation_count: int,
        confirmation_required: int,
        entry_confirmation_pct: float,
        invalidation_pct: float,
    ) -> Dict[str, Any]:
        baseline = PriceSnapshot(
            timestamp=opportunity.baseline_timestamp,
            price=opportunity.baseline_price,
            provider_timestamp=opportunity.provider_timestamp,
        )
        change_pct = (snapshot.price - opportunity.baseline_price) / opportunity.baseline_price * 100
        fields = cls._market_fields(
            snapshot=snapshot,
            baseline=baseline,
            change_pct=change_pct,
            threshold_pct=opportunity.threshold_pct,
            confirmation_count=confirmation_count,
            confirmation_required=confirmation_required,
        )
        trade_direction = "long" if opportunity.direction == "up" else "short"
        if opportunity.direction == "up":
            entry_price = opportunity.opportunity_price * (1 + entry_confirmation_pct / 100)
            invalidation_price = opportunity.opportunity_price * (1 - invalidation_pct / 100)
        else:
            entry_price = opportunity.opportunity_price * (1 - entry_confirmation_pct / 100)
            invalidation_price = opportunity.opportunity_price * (1 + invalidation_pct / 100)
        fields.update(
            {
                "opportunity_state": "active",
                "opportunity_direction": opportunity.direction,
                "trade_direction": trade_direction,
                "suggested_trade_action": f"{trade_direction}_entry",
                "opportunity_price": round(opportunity.opportunity_price, 4),
                "initial_change_pct": round(opportunity.initial_change_pct, 4),
                "entry_confirmation_pct": round(entry_confirmation_pct, 4),
                "entry_price": round(entry_price, 4),
                "invalidation_pct": round(invalidation_pct, 4),
                "invalidation_price": round(invalidation_price, 4),
                "watched_seconds": max(0, int(snapshot.timestamp - opportunity.detected_at)),
            }
        )
        return fields
