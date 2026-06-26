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


class BTCVolatilityMonitor:
    """Track recent BTC quotes and decide when a fresh hourly analysis is warranted."""

    def __init__(
        self,
        *,
        quote_fetcher: Optional[Callable[[str], Any]] = None,
        now_provider: Optional[Callable[[], float]] = None,
    ) -> None:
        self.quote_fetcher = quote_fetcher or self._fetch_quote
        self.now_provider = now_provider or time.time
        self._snapshots: List[PriceSnapshot] = []
        self._last_trigger_at: Optional[float] = None

    @staticmethod
    def _fetch_quote(symbol: str) -> Any:
        return CryptoFetcher().get_realtime_quote(symbol)

    def run_once(self, config: Any) -> Dict[str, Any]:
        """Check one quote and return trigger metadata for the scheduler."""
        stats: Dict[str, Any] = {
            "checked": 0,
            "triggered": 0,
            "suppressed": 0,
            "reason": "",
        }
        if not getattr(config, "btc_volatility_monitor_enabled", False):
            stats["reason"] = "disabled"
            return stats

        symbol = str(getattr(config, "btc_volatility_monitor_symbol", "BTC") or "BTC").strip() or "BTC"
        now = float(self.now_provider())
        quote = self.quote_fetcher(symbol)
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

        if len(self._snapshots) < 2:
            stats["reason"] = "warming_up"
            stats.update(self._snapshot_fields(snapshot))
            return stats

        baseline = self._snapshots[0]
        change_pct = (snapshot.price - baseline.price) / baseline.price * 100
        if abs(change_pct) < threshold_pct:
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

        if self._last_trigger_at is not None and now - self._last_trigger_at < cooldown_seconds:
            stats["suppressed"] = 1
            stats["reason"] = "cooldown"
            stats.update(
                self._market_fields(
                    snapshot=snapshot,
                    baseline=baseline,
                    change_pct=change_pct,
                    threshold_pct=threshold_pct,
                )
            )
            return stats

        self._last_trigger_at = now
        stats["triggered"] = 1
        stats["reason"] = "volatility_threshold"
        stats.update(
            self._market_fields(
                snapshot=snapshot,
                baseline=baseline,
                change_pct=change_pct,
                threshold_pct=threshold_pct,
            )
        )
        return stats

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
        return fields
