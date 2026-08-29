# -*- coding: utf-8 -*-
"""BTC price-volatility trigger for event-driven hourly analysis."""

from __future__ import annotations

import logging
import math
import statistics
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from data_provider.crypto_fetcher import CryptoFetcher

logger = logging.getLogger(__name__)

# Minimum recent samples before adaptive thresholds / velocity baseline engage;
# below this the configured static thresholds are used unchanged.
_ADAPTIVE_MIN_SAMPLES = 30


@dataclass(frozen=True)
class PriceSnapshot:
    timestamp: float
    price: float
    provider_timestamp: Optional[str] = None


@dataclass(frozen=True)
class WindowTier:
    """One detection window: breach when the move inside reaches threshold_pct."""

    window_seconds: int
    threshold_pct: float


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
    tier_window_seconds: Optional[int] = None
    velocity_trigger: bool = False
    fast_path: bool = False  # violent move: eligible for single-sample confirmation
    peak_price: Optional[float] = None
    trough_price: Optional[float] = None


def _parse_window_tiers(raw: Any) -> List[WindowTier]:
    """Parse "1:0.4,3:0.7,5:1.0" (minutes:threshold_pct) into sorted tiers.

    Invalid entries are dropped; an empty/invalid string yields an empty list,
    which keeps the monitor on the legacy single-window code path.
    """
    if not isinstance(raw, str) or not raw.strip():
        return []
    tiers: List[WindowTier] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        minutes_raw, threshold_raw = chunk.split(":", 1)
        try:
            minutes = float(minutes_raw.strip())
            threshold = float(threshold_raw.strip())
        except ValueError:
            continue
        if minutes <= 0 or threshold < 0.1:
            continue
        tiers.append(WindowTier(window_seconds=max(30, int(minutes * 60)), threshold_pct=threshold))
    tiers.sort(key=lambda tier: tier.window_seconds)
    return tiers


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
        self._last_trigger_direction: Optional[str] = None
        self._confirmation_direction: Optional[str] = None
        self._confirmation_count = 0
        self._early_warning_direction: Optional[str] = None
        self._active_opportunity: Optional[ActiveOpportunity] = None
        self._last_quote_error_log_at: Optional[float] = None
        self._last_sweep_extreme_ts: Optional[float] = None
        # (timestamp, ret_pct, abs_rate_pct_per_second) of recent poll-to-poll
        # moves; feeds adaptive thresholds and the velocity baseline.
        self._stat_samples: List[Tuple[float, float, float]] = []
        self._last_stat_snapshot: Optional[PriceSnapshot] = None

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
        allow_reversal_bypass = bool(
            getattr(config, "btc_volatility_monitor_cooldown_allow_reversal", False)
        )
        spike_revert_pct = max(
            0.1,
            float(getattr(config, "btc_volatility_monitor_spike_revert_pct", 0.4) or 0.4),
        )
        adaptive_enabled = bool(getattr(config, "btc_volatility_monitor_adaptive_threshold_enabled", False))
        adaptive_k = max(
            0.1,
            float(getattr(config, "btc_volatility_monitor_adaptive_k", 2.5) or 2.5),
        )
        adaptive_min_pct = max(
            0.1,
            float(getattr(config, "btc_volatility_monitor_adaptive_min_pct", 0.4) or 0.4),
        )
        adaptive_max_pct = max(
            adaptive_min_pct,
            float(getattr(config, "btc_volatility_monitor_adaptive_max_pct", 2.0) or 2.0),
        )
        adaptive_lookback_seconds = max(
            600,
            int(getattr(config, "btc_volatility_monitor_adaptive_lookback_minutes", 240) or 240) * 60,
        )
        velocity_enabled = bool(getattr(config, "btc_volatility_monitor_velocity_enabled", False))
        velocity_mult = max(
            1.0,
            float(getattr(config, "btc_volatility_monitor_velocity_mult", 3.0) or 3.0),
        )
        velocity_min_pct = max(
            0.01,
            float(getattr(config, "btc_volatility_monitor_velocity_min_pct", 0.1) or 0.1),
        )
        fast_confirmation_enabled = bool(
            getattr(config, "btc_volatility_monitor_fast_confirmation_enabled", False)
        )
        fast_confirmation_mult = max(
            1.0,
            float(getattr(config, "btc_volatility_monitor_fast_confirmation_mult", 1.5) or 1.5),
        )
        max_entry_overshoot_pct = max(
            0.0,
            float(getattr(config, "btc_volatility_monitor_max_entry_overshoot_pct", 0.3) or 0.0),
        )
        exhaustion_retrace_pct = max(
            0.05,
            float(getattr(config, "btc_volatility_monitor_exhaustion_retrace_pct", 0.25) or 0.25),
        )
        tiers = _parse_window_tiers(getattr(config, "btc_volatility_monitor_window_tiers", ""))
        if tiers:
            window_seconds = max(tier.window_seconds for tier in tiers)
        else:
            window_seconds = max(
                30,
                int(getattr(config, "btc_volatility_monitor_window_minutes", 5) or 5) * 60,
            )
        # In tiered mode the velocity floor scales with the fastest tier
        # threshold: a poll-to-poll jitter far below the smallest window
        # breakout is noise, not an impulse (fixes reversal flip-flops).
        velocity_floor_pct = velocity_min_pct
        if tiers:
            velocity_floor_pct = max(
                velocity_floor_pct,
                0.5 * min(tier.threshold_pct for tier in tiers),
            )

        snapshot = PriceSnapshot(
            timestamp=now,
            price=float(price),
            provider_timestamp=self._quote_provider_timestamp(quote),
        )
        self._snapshots.append(snapshot)
        self._prune(now=now, window_seconds=window_seconds)
        if adaptive_enabled or velocity_enabled:
            self._record_sample_stats(snapshot=snapshot, now=now, lookback_seconds=adaptive_lookback_seconds)

        active_stats = self._evaluate_active_opportunity(
            snapshot=snapshot,
            now=now,
            confirmation_samples=confirmation_samples,
            entry_confirmation_pct=entry_confirmation_pct,
            invalidation_pct=invalidation_pct,
            max_watch_seconds=max_watch_seconds,
            cooldown_seconds=cooldown_seconds,
            allow_reversal_bypass=allow_reversal_bypass,
            fast_confirmation_enabled=fast_confirmation_enabled,
            max_entry_overshoot_pct=max_entry_overshoot_pct,
            exhaustion_retrace_pct=exhaustion_retrace_pct,
        )
        if active_stats is not None:
            stats.update(active_stats)
            return stats

        if len(self._snapshots) < 2:
            stats["reason"] = "warming_up"
            stats.update(self._snapshot_fields(snapshot))
            return stats

        velocity_hit = self._velocity_hit(
            now=now,
            lookback_seconds=adaptive_lookback_seconds,
            mult=velocity_mult,
            min_pct=velocity_floor_pct,
        ) if velocity_enabled else None

        if tiers:
            effective_tiers = [
                WindowTier(
                    window_seconds=tier.window_seconds,
                    threshold_pct=self._effective_threshold_pct(
                        static_pct=tier.threshold_pct,
                        window_seconds=tier.window_seconds,
                        now=now,
                        lookback_seconds=adaptive_lookback_seconds,
                        enabled=adaptive_enabled,
                        k=adaptive_k,
                        min_pct=adaptive_min_pct,
                        max_pct=adaptive_max_pct,
                    ),
                )
                for tier in tiers
            ]
            stats.update(
                self._evaluate_tiered_windows(
                    snapshot=snapshot,
                    tiers=effective_tiers,
                    early_warning_pct=early_warning_pct,
                    spike_revert_pct=spike_revert_pct,
                    entry_confirmation_pct=entry_confirmation_pct,
                    invalidation_pct=invalidation_pct,
                    confirmation_samples=confirmation_samples,
                    velocity_hit=velocity_hit,
                    fast_confirmation_mult=fast_confirmation_mult,
                )
            )
            return stats

        threshold_pct = self._effective_threshold_pct(
            static_pct=threshold_pct,
            window_seconds=window_seconds,
            now=now,
            lookback_seconds=adaptive_lookback_seconds,
            enabled=adaptive_enabled,
            k=adaptive_k,
            min_pct=adaptive_min_pct,
            max_pct=adaptive_max_pct,
        )
        early_warning_pct = min(threshold_pct, early_warning_pct)

        baseline = self._snapshots[0]
        change_pct = (snapshot.price - baseline.price) / baseline.price * 100
        direction = "up" if change_pct > 0 else "down"
        if velocity_hit is not None and abs(change_pct) < threshold_pct:
            velocity_direction, velocity_change_pct = velocity_hit
            velocity_baseline = self._snapshots[-2] if len(self._snapshots) >= 2 else snapshot
            self._early_warning_direction = None
            # Velocity opportunities never take the fast path: their direction
            # is a single poll-to-poll return, so they keep full confirmation.
            self._active_opportunity = ActiveOpportunity(
                direction=velocity_direction,
                detected_at=now,
                baseline_timestamp=velocity_baseline.timestamp,
                baseline_price=velocity_baseline.price,
                opportunity_price=snapshot.price,
                threshold_pct=threshold_pct,
                initial_change_pct=velocity_change_pct,
                provider_timestamp=snapshot.provider_timestamp,
                velocity_trigger=True,
            )
            self._confirmation_direction = velocity_direction
            self._confirmation_count = 1
            stats["reason"] = "awaiting_confirmation"
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
            fast_path=abs(change_pct) >= fast_confirmation_mult * threshold_pct,
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

    def _evaluate_tiered_windows(
        self,
        *,
        snapshot: PriceSnapshot,
        tiers: List[WindowTier],
        early_warning_pct: float,
        spike_revert_pct: float,
        entry_confirmation_pct: float,
        invalidation_pct: float,
        confirmation_samples: int,
        velocity_hit: Optional[Tuple[str, float]] = None,
        fast_confirmation_mult: float = 1.5,
    ) -> Dict[str, Any]:
        """Multi-window detection: the shortest breached tier wins; faded spikes
        are reported as liquidity sweeps instead of silently ageing out."""
        now = snapshot.timestamp
        tier_moves: List[Tuple[WindowTier, PriceSnapshot, float]] = []
        breached: Optional[Tuple[WindowTier, PriceSnapshot, float]] = None
        for tier in tiers:
            baseline = self._oldest_since(now - tier.window_seconds)
            if baseline is None:
                continue
            span = snapshot.timestamp - baseline.timestamp
            if span < tier.window_seconds * 0.5:
                continue
            change_pct = (snapshot.price - baseline.price) / baseline.price * 100
            tier_moves.append((tier, baseline, change_pct))
            if breached is None and abs(change_pct) >= tier.threshold_pct:
                breached = (tier, baseline, change_pct)

        if breached is not None:
            tier, baseline, change_pct = breached
            direction = "up" if change_pct > 0 else "down"
            self._early_warning_direction = None
            self._active_opportunity = ActiveOpportunity(
                direction=direction,
                detected_at=now,
                baseline_timestamp=baseline.timestamp,
                baseline_price=baseline.price,
                opportunity_price=snapshot.price,
                threshold_pct=tier.threshold_pct,
                initial_change_pct=change_pct,
                provider_timestamp=snapshot.provider_timestamp,
                tier_window_seconds=tier.window_seconds,
                fast_path=abs(change_pct) >= fast_confirmation_mult * tier.threshold_pct,
            )
            self._confirmation_direction = direction
            self._confirmation_count = 1
            fields = self._opportunity_fields(
                snapshot=snapshot,
                opportunity=self._active_opportunity,
                confirmation_count=self._confirmation_count,
                confirmation_required=confirmation_samples,
                entry_confirmation_pct=entry_confirmation_pct,
                invalidation_pct=invalidation_pct,
            )
            fields["reason"] = "awaiting_confirmation"
            fields["event_detected"] = 1
            fields["trigger_reason"] = "volatility_spike"
            return fields

        if velocity_hit is not None:
            direction, velocity_change_pct = velocity_hit
            reference_threshold = min(tier.threshold_pct for tier in tiers)
            velocity_baseline = self._snapshots[-2] if len(self._snapshots) >= 2 else snapshot
            self._early_warning_direction = None
            # Velocity opportunities never take the fast path: their direction
            # is a single poll-to-poll return, so they keep full confirmation.
            self._active_opportunity = ActiveOpportunity(
                direction=direction,
                detected_at=now,
                baseline_timestamp=velocity_baseline.timestamp,
                baseline_price=velocity_baseline.price,
                opportunity_price=snapshot.price,
                threshold_pct=reference_threshold,
                initial_change_pct=velocity_change_pct,
                provider_timestamp=snapshot.provider_timestamp,
                velocity_trigger=True,
            )
            self._confirmation_direction = direction
            self._confirmation_count = 1
            fields = self._opportunity_fields(
                snapshot=snapshot,
                opportunity=self._active_opportunity,
                confirmation_count=self._confirmation_count,
                confirmation_required=confirmation_samples,
                entry_confirmation_pct=entry_confirmation_pct,
                invalidation_pct=invalidation_pct,
            )
            fields["reason"] = "awaiting_confirmation"
            fields["event_detected"] = 1
            fields["trigger_reason"] = "volatility_spike"
            return fields

        sweep = self._detect_liquidity_sweep(
            snapshot=snapshot,
            tiers=tiers,
            spike_revert_pct=spike_revert_pct,
        )
        if sweep is not None:
            self._early_warning_direction = None
            return sweep

        if tier_moves:
            tier, baseline, change_pct = tier_moves[0]
            if abs(change_pct) >= early_warning_pct:
                direction = "up" if change_pct > 0 else "down"
                fields = self._early_warning_fields(
                    snapshot=snapshot,
                    baseline=baseline,
                    change_pct=change_pct,
                    threshold_pct=tier.threshold_pct,
                    early_warning_pct=early_warning_pct,
                    entry_confirmation_pct=entry_confirmation_pct,
                    invalidation_pct=invalidation_pct,
                )
                fields["reason"] = "early_warning_active"
                if self._early_warning_direction != direction:
                    self._early_warning_direction = direction
                    fields["early_warning_detected"] = 1
                    fields["reason"] = "early_warning"
                    fields["trigger_reason"] = "early_warning"
                return fields

        self._early_warning_direction = None
        self._reset_confirmation()
        if tier_moves:
            tier, baseline, change_pct = tier_moves[-1]
            fields = self._market_fields(
                snapshot=snapshot,
                baseline=baseline,
                change_pct=change_pct,
                threshold_pct=tier.threshold_pct,
            )
            fields["reason"] = "below_threshold"
            return fields
        fields = self._snapshot_fields(snapshot)
        fields["reason"] = "below_threshold"
        return fields

    def _detect_liquidity_sweep(
        self,
        *,
        snapshot: PriceSnapshot,
        tiers: List[WindowTier],
        spike_revert_pct: float,
    ) -> Optional[Dict[str, Any]]:
        """Report a spike that reached the fastest tier threshold and already faded.

        A fast wick that reverts between polls never crosses the current-price
        thresholds, so without this check the move is invisible. The event is
        analysis-triggering but not an entry: a failed breakout starts the
        right-side state machine and must not be turned into an immediate order.
        """
        if len(self._snapshots) < 3:
            return None
        baseline = self._snapshots[0]
        if baseline.price <= 0:
            return None
        sweep_threshold_pct = min(tier.threshold_pct for tier in tiers)
        window_high = max(self._snapshots, key=lambda item: item.price)
        window_low = min(self._snapshots, key=lambda item: item.price)
        for side, extreme in (("up", window_high), ("down", window_low)):
            if extreme.timestamp >= snapshot.timestamp:
                continue
            if extreme.timestamp == self._last_sweep_extreme_ts:
                continue
            if side == "up":
                runup_pct = (extreme.price - baseline.price) / baseline.price * 100
                revert_pct = (extreme.price - snapshot.price) / extreme.price * 100
            else:
                runup_pct = (baseline.price - extreme.price) / baseline.price * 100
                revert_pct = (snapshot.price - extreme.price) / extreme.price * 100
            if runup_pct < sweep_threshold_pct or revert_pct < spike_revert_pct:
                continue
            self._last_sweep_extreme_ts = extreme.timestamp
            trade_direction = "short" if side == "up" else "long"
            change_pct = (snapshot.price - baseline.price) / baseline.price * 100
            fields = self._market_fields(
                snapshot=snapshot,
                baseline=baseline,
                change_pct=change_pct,
                threshold_pct=sweep_threshold_pct,
            )
            fields.update(
                {
                    "reason": "liquidity_sweep",
                    "trigger_reason": "liquidity_sweep",
                    "event_detected": 1,
                    "opportunity_state": "sweep_detected",
                    "sweep_side": side,
                    "swept_extreme_price": round(extreme.price, 4),
                    "sweep_runup_pct": round(runup_pct, 4),
                    "revert_pct": round(revert_pct, 4),
                    "spike_revert_pct": round(spike_revert_pct, 4),
                    "right_side_state": "sweep_detected",
                    "right_side_direction": trade_direction,
                    "right_side_trial_position_pct": 25,
                    "right_side_retest_required": False,
                    "right_side_confirmation_add_requires_retest": True,
                    "trade_direction": trade_direction,
                    "suggested_trade_action": f"watch_{trade_direction}_after_sweep",
                }
            )
            return fields
        return None

    def _oldest_since(self, cutoff: float) -> Optional[PriceSnapshot]:
        for snapshot in self._snapshots:
            if snapshot.timestamp >= cutoff:
                return snapshot
        return None

    def _record_sample_stats(self, *, snapshot: PriceSnapshot, now: float, lookback_seconds: int) -> None:
        previous = self._last_stat_snapshot
        self._last_stat_snapshot = snapshot
        if previous is not None and previous.price > 0:
            dt = snapshot.timestamp - previous.timestamp
            if dt > 0:
                ret_pct = (snapshot.price - previous.price) / previous.price * 100
                self._stat_samples.append((snapshot.timestamp, ret_pct, abs(ret_pct) / dt))
        cutoff = now - lookback_seconds
        self._stat_samples = [sample for sample in self._stat_samples if sample[0] >= cutoff]

    def _velocity_hit(
        self,
        *,
        now: float,
        lookback_seconds: int,
        mult: float,
        min_pct: float,
    ) -> Optional[Tuple[str, float]]:
        """Return (direction, ret_pct) when the latest poll-to-poll move rate is
        at least ``mult`` times the recent median rate, else None.

        ``min_pct`` is an absolute floor on the move itself: in quiet regimes
        the median rate collapses towards zero and a pure relative multiplier
        would flag plain noise.
        """
        cutoff = now - lookback_seconds
        samples = [sample for sample in self._stat_samples if sample[0] <= now and sample[0] >= cutoff]
        if len(samples) < _ADAPTIVE_MIN_SAMPLES + 1:
            return None
        latest_ts, latest_ret, latest_rate = samples[-1]
        if abs(latest_ret) < min_pct:
            return None
        history = [sample[2] for sample in samples[:-1] if sample[0] < latest_ts]
        if len(history) < _ADAPTIVE_MIN_SAMPLES:
            return None
        median_rate = statistics.median(history)
        if median_rate <= 0 or latest_rate < mult * median_rate:
            return None
        direction = "up" if latest_ret > 0 else "down"
        return (direction, latest_ret)

    def _effective_threshold_pct(
        self,
        *,
        static_pct: float,
        window_seconds: int,
        now: float,
        lookback_seconds: int,
        enabled: bool,
        k: float,
        min_pct: float,
        max_pct: float,
    ) -> float:
        """Adaptive threshold: clamp(k * sigma * sqrt(window/dt), min, max).

        sigma is the stdev of recent poll-to-poll returns and dt the median poll
        interval, so the estimate scales with the detection window. Falls back
        to the static threshold while the sample history is too thin.
        """
        if not enabled:
            return static_pct
        cutoff = now - lookback_seconds
        samples = [sample for sample in self._stat_samples if sample[0] <= now and sample[0] >= cutoff]
        if len(samples) < _ADAPTIVE_MIN_SAMPLES:
            return static_pct
        sigma = statistics.pstdev([sample[1] for sample in samples])
        intervals = [
            samples[index][0] - samples[index - 1][0]
            for index in range(1, len(samples))
        ]
        dt = statistics.median(intervals) if intervals else 0.0
        if sigma <= 0 or dt <= 0:
            return static_pct
        estimate = k * sigma * math.sqrt(window_seconds / dt)
        return max(min_pct, min(max_pct, estimate))

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
        allow_reversal_bypass: bool,
        fast_confirmation_enabled: bool = False,
        max_entry_overshoot_pct: float = 0.3,
        exhaustion_retrace_pct: float = 0.25,
    ) -> Optional[Dict[str, Any]]:
        opportunity = self._active_opportunity
        if opportunity is None:
            return None

        confirmation_required = confirmation_samples
        if fast_confirmation_enabled and opportunity.fast_path:
            # violent moves (velocity trigger or >=fast_mult x threshold) confirm
            # on a single sample: timeliness beats noise filtering here.
            confirmation_required = 1

        watched_seconds = max(0, int(now - opportunity.detected_at))
        if watched_seconds > max_watch_seconds:
            fields = self._opportunity_fields(
                snapshot=snapshot,
                opportunity=opportunity,
                confirmation_count=self._confirmation_count,
                confirmation_required=confirmation_required,
                entry_confirmation_pct=entry_confirmation_pct,
                invalidation_pct=invalidation_pct,
            )
            fields["reason"] = "watch_expired"
            self._reset_confirmation()
            return fields

        self._record_opportunity_extreme(snapshot, opportunity)

        if self._is_opportunity_invalidated(snapshot, opportunity, invalidation_pct):
            fields = self._opportunity_fields(
                snapshot=snapshot,
                opportunity=opportunity,
                confirmation_count=self._confirmation_count,
                confirmation_required=confirmation_required,
                entry_confirmation_pct=entry_confirmation_pct,
                invalidation_pct=invalidation_pct,
            )
            fields["reason"] = "opportunity_invalidated"
            self._reset_confirmation()
            return fields

        if self._is_opportunity_exhausted(
            snapshot,
            opportunity,
            entry_confirmation_pct=entry_confirmation_pct,
            exhaustion_retrace_pct=exhaustion_retrace_pct,
        ):
            fields = self._opportunity_fields(
                snapshot=snapshot,
                opportunity=opportunity,
                confirmation_count=self._confirmation_count,
                confirmation_required=confirmation_required,
                entry_confirmation_pct=entry_confirmation_pct,
                invalidation_pct=invalidation_pct,
            )
            fields.update(
                self._execution_fields(
                    snapshot=snapshot,
                    opportunity=opportunity,
                    entry_confirmation_pct=entry_confirmation_pct,
                    max_entry_overshoot_pct=max_entry_overshoot_pct,
                    exhaustion_retrace_pct=exhaustion_retrace_pct,
                    entry_confirmed=False,
                )
            )
            fields.update(
                {
                    "reason": "impulse_exhausted",
                    "trigger_reason": "impulse_exhausted",
                    "event_detected": 1,
                    "suggested_trade_action": "wait_after_impulse_exhaustion",
                }
            )
            self._reset_confirmation()
            return fields

        entry_confirmed = self._entry_signal_confirmed(snapshot, opportunity, entry_confirmation_pct)
        if not entry_confirmed:
            fields = self._opportunity_fields(
                snapshot=snapshot,
                opportunity=opportunity,
                confirmation_count=self._confirmation_count,
                confirmation_required=confirmation_required,
                entry_confirmation_pct=entry_confirmation_pct,
                invalidation_pct=invalidation_pct,
            )
            fields.update(
                self._execution_fields(
                    snapshot=snapshot,
                    opportunity=opportunity,
                    entry_confirmation_pct=entry_confirmation_pct,
                    max_entry_overshoot_pct=max_entry_overshoot_pct,
                    exhaustion_retrace_pct=exhaustion_retrace_pct,
                    entry_confirmed=False,
                )
            )
            fields["reason"] = "watching_opportunity"
            return fields

        if self._confirmation_direction == opportunity.direction:
            self._confirmation_count += 1
        else:
            self._confirmation_direction = opportunity.direction
            self._confirmation_count = 1

        fields = self._opportunity_fields(
            snapshot=snapshot,
            opportunity=opportunity,
            confirmation_count=self._confirmation_count,
            confirmation_required=confirmation_required,
            entry_confirmation_pct=entry_confirmation_pct,
            invalidation_pct=invalidation_pct,
        )
        fields.update(
            self._execution_fields(
                snapshot=snapshot,
                opportunity=opportunity,
                entry_confirmation_pct=entry_confirmation_pct,
                max_entry_overshoot_pct=max_entry_overshoot_pct,
                exhaustion_retrace_pct=exhaustion_retrace_pct,
                entry_confirmed=True,
            )
        )
        if self._confirmation_count < confirmation_required:
            fields["reason"] = "watching_opportunity"
            return fields

        if self._last_trigger_at is not None and now - self._last_trigger_at < cooldown_seconds:
            reversal_bypass = (
                allow_reversal_bypass
                and self._last_trigger_direction is not None
                and opportunity.direction != self._last_trigger_direction
                # Only real window breaches may break the cooldown; a velocity
                # reversal is noise-level and must wait the cooldown out.
                and not opportunity.velocity_trigger
            )
            if not reversal_bypass:
                fields["suppressed"] = 1
                fields["reason"] = "cooldown"
                return fields
            fields["cooldown_bypassed"] = 1

        self._last_trigger_at = now
        self._last_trigger_direction = opportunity.direction
        fields["triggered"] = 1
        fields["reason"] = "entry_signal"
        fields["trigger_reason"] = "entry_signal"
        self._reset_confirmation()
        return fields

    @staticmethod
    def _record_opportunity_extreme(
        snapshot: PriceSnapshot,
        opportunity: ActiveOpportunity,
    ) -> None:
        opportunity.peak_price = max(
            value for value in (opportunity.peak_price, opportunity.opportunity_price, snapshot.price)
            if value is not None
        )
        opportunity.trough_price = min(
            value for value in (opportunity.trough_price, opportunity.opportunity_price, snapshot.price)
            if value is not None
        )

    @staticmethod
    def _is_opportunity_exhausted(
        snapshot: PriceSnapshot,
        opportunity: ActiveOpportunity,
        *,
        entry_confirmation_pct: float,
        exhaustion_retrace_pct: float,
    ) -> bool:
        """Reject a failed vertical impulse before it becomes a chase entry."""
        entry_price = (
            opportunity.opportunity_price * (1 + entry_confirmation_pct / 100)
            if opportunity.direction == "up"
            else opportunity.opportunity_price * (1 - entry_confirmation_pct / 100)
        )
        if opportunity.direction == "up":
            peak = opportunity.peak_price or opportunity.opportunity_price
            retrace_pct = (peak - snapshot.price) / peak * 100 if peak > 0 else 0.0
            return peak >= entry_price and retrace_pct >= exhaustion_retrace_pct
        trough = opportunity.trough_price or opportunity.opportunity_price
        retrace_pct = (snapshot.price - trough) / trough * 100 if trough > 0 else 0.0
        return trough <= entry_price and retrace_pct >= exhaustion_retrace_pct

    @staticmethod
    def _execution_fields(
        *,
        snapshot: PriceSnapshot,
        opportunity: ActiveOpportunity,
        entry_confirmation_pct: float,
        max_entry_overshoot_pct: float,
        exhaustion_retrace_pct: float,
        entry_confirmed: bool,
    ) -> Dict[str, Any]:
        """Describe whether a confirmed impulse is still executable at this price.

        The monitor intentionally does not manufacture a lower/higher "ideal"
        entry after a vertical move. A report may describe a conditional retest,
        but only a price close to the live confirmation level is executable now.
        """
        if opportunity.direction == "up":
            entry_price = opportunity.opportunity_price * (1 + entry_confirmation_pct / 100)
            overshoot_pct = max(0.0, (snapshot.price - entry_price) / entry_price * 100)
            no_chase_price = entry_price * (1 + max_entry_overshoot_pct / 100)
            extreme_price = opportunity.peak_price or opportunity.opportunity_price
            retrace_pct = (extreme_price - snapshot.price) / extreme_price * 100 if extreme_price > 0 else 0.0
        else:
            entry_price = opportunity.opportunity_price * (1 - entry_confirmation_pct / 100)
            overshoot_pct = max(0.0, (entry_price - snapshot.price) / entry_price * 100)
            no_chase_price = entry_price * (1 - max_entry_overshoot_pct / 100)
            extreme_price = opportunity.trough_price or opportunity.opportunity_price
            retrace_pct = (snapshot.price - extreme_price) / extreme_price * 100 if extreme_price > 0 else 0.0

        # A candidate can have crossed the confirmation price earlier and then
        # faded before it became tradeable. Exhaustion must take precedence
        # over the current price no longer satisfying the confirmation test.
        exhausted = retrace_pct >= exhaustion_retrace_pct
        late_extension = entry_confirmed and overshoot_pct > max_entry_overshoot_pct
        if exhausted:
            impulse_stage = "exhaustion_candidate"
        elif late_extension:
            impulse_stage = "late_extension"
        elif entry_confirmed:
            impulse_stage = "early_continuation"
        else:
            impulse_stage = "first_impulse_candidate"

        return {
            "impulse_stage": impulse_stage,
            "entry_executable_now": int(entry_confirmed and not late_extension and not exhausted),
            "entry_overshoot_pct": round(overshoot_pct, 4),
            "max_entry_overshoot_pct": round(max_entry_overshoot_pct, 4),
            "no_chase_price": round(no_chase_price, 4),
            "impulse_extreme_price": round(extreme_price, 4),
            "impulse_retrace_pct": round(retrace_pct, 4),
            "exhaustion_retrace_pct": round(exhaustion_retrace_pct, 4),
        }

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
                "impulse_stage": "first_impulse_candidate",
                "entry_executable_now": 0,
            }
        )
        if opportunity.tier_window_seconds is not None:
            fields["tier_window_seconds"] = int(opportunity.tier_window_seconds)
        if opportunity.velocity_trigger:
            fields["velocity_trigger"] = 1
        if opportunity.fast_path:
            fields["fast_path"] = 1
        return fields
