# -*- coding: utf-8 -*-
"""Tests for BTC volatility-triggered analysis monitor."""

from __future__ import annotations

from types import SimpleNamespace

from src.services.btc_volatility_monitor import (
    ActiveOpportunity,
    BTCVolatilityMonitor,
    _parse_window_tiers,
)


def _config(**overrides):
    defaults = {
        "btc_volatility_monitor_enabled": True,
        "btc_volatility_monitor_symbol": "BTC",
        "btc_volatility_monitor_window_minutes": 5,
        "btc_volatility_monitor_early_warning_pct": 0.3,
        "btc_volatility_monitor_threshold_pct": 1.0,
        "btc_volatility_monitor_cooldown_minutes": 30,
        "btc_volatility_monitor_confirmation_samples": 2,
        "btc_volatility_monitor_entry_confirmation_pct": 0.2,
        "btc_volatility_monitor_invalidation_pct": 0.5,
        "btc_volatility_monitor_max_watch_minutes": 20,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_btc_volatility_monitor_triggers_when_window_move_exceeds_threshold() -> None:
    prices = iter([100.0, 101.2, 101.5])
    times = iter([1000.0, 1060.0, 1120.0])
    monitor = BTCVolatilityMonitor(
        quote_fetcher=lambda _symbol: {"price": next(prices), "provider_timestamp": "quote-ts"},
        now_provider=lambda: next(times),
    )

    warmup = monitor.run_once(_config())
    awaiting_confirmation = monitor.run_once(_config())
    triggered = monitor.run_once(_config())

    assert warmup["reason"] == "warming_up"
    assert awaiting_confirmation["reason"] == "awaiting_confirmation"
    assert awaiting_confirmation["event_detected"] == 1
    assert awaiting_confirmation["trigger_reason"] == "volatility_spike"
    assert awaiting_confirmation["confirmation_count"] == 1
    assert awaiting_confirmation["confirmation_required"] == 2
    assert triggered["triggered"] == 1
    assert triggered["event_detected"] == 0
    assert triggered["reason"] == "entry_signal"
    assert triggered["trigger_reason"] == "entry_signal"
    assert triggered["direction"] == "up"
    assert triggered["trade_direction"] == "long"
    assert triggered["suggested_trade_action"] == "long_entry"
    assert triggered["change_pct"] == 1.5
    assert triggered["baseline_price"] == 100.0
    assert triggered["opportunity_price"] == 101.2
    assert triggered["entry_price"] == 101.4024
    assert triggered["invalidation_price"] == 100.694
    assert triggered["price"] == 101.5


def test_btc_volatility_monitor_suppresses_retrigger_during_cooldown() -> None:
    prices = iter([100.0, 101.2, 101.5, 102.8, 103.2])
    times = iter([1000.0, 1060.0, 1120.0, 1180.0, 1240.0])
    monitor = BTCVolatilityMonitor(
        quote_fetcher=lambda _symbol: {"price": next(prices)},
        now_provider=lambda: next(times),
    )

    monitor.run_once(_config())
    monitor.run_once(_config())
    first = monitor.run_once(_config())
    monitor.run_once(_config())
    second = monitor.run_once(_config())

    assert first["triggered"] == 1
    assert second["triggered"] == 0
    assert second["suppressed"] == 1
    assert second["reason"] == "cooldown"


def test_btc_volatility_monitor_sends_one_early_warning_before_full_threshold() -> None:
    prices = iter([100.0, 99.6, 99.5])
    times = iter([1000.0, 1060.0, 1120.0])
    monitor = BTCVolatilityMonitor(
        quote_fetcher=lambda _symbol: {"price": next(prices)},
        now_provider=lambda: next(times),
    )

    monitor.run_once(_config())
    early_warning = monitor.run_once(_config())
    still_active = monitor.run_once(_config())

    assert early_warning["reason"] == "early_warning"
    assert early_warning["early_warning_detected"] == 1
    assert early_warning["trigger_reason"] == "early_warning"
    assert early_warning["threshold_price"] == 99.0
    assert early_warning["entry_price"] == 98.802
    assert early_warning["invalidation_price"] == 99.495
    assert still_active["reason"] == "early_warning_active"
    assert not still_active.get("early_warning_detected")


def test_btc_volatility_monitor_prunes_old_baseline() -> None:
    prices = iter([100.0, 100.2, 101.4, 101.7])
    times = iter([1000.0, 1400.0, 1460.0, 1520.0])
    monitor = BTCVolatilityMonitor(
        quote_fetcher=lambda _symbol: {"price": next(prices)},
        now_provider=lambda: next(times),
    )

    monitor.run_once(_config(btc_volatility_monitor_window_minutes=5))
    warmup_after_prune = monitor.run_once(_config(btc_volatility_monitor_window_minutes=5))
    awaiting_confirmation = monitor.run_once(_config(btc_volatility_monitor_window_minutes=5))
    triggered = monitor.run_once(_config(btc_volatility_monitor_window_minutes=5))

    assert warmup_after_prune["reason"] == "warming_up"
    assert awaiting_confirmation["reason"] == "awaiting_confirmation"
    assert triggered["triggered"] == 1
    assert triggered["baseline_price"] == 100.2


def test_btc_volatility_monitor_resets_confirmation_when_move_fades() -> None:
    prices = iter([100.0, 101.2, 100.4, 101.3])
    times = iter([1000.0, 1060.0, 1120.0, 1180.0])
    monitor = BTCVolatilityMonitor(
        quote_fetcher=lambda _symbol: {"price": next(prices)},
        now_provider=lambda: next(times),
    )

    monitor.run_once(_config())
    first_hit = monitor.run_once(_config())
    faded = monitor.run_once(_config())
    next_hit = monitor.run_once(_config())

    assert first_hit["reason"] == "awaiting_confirmation"
    assert faded["reason"] == "opportunity_invalidated"
    assert next_hit["reason"] == "awaiting_confirmation"
    assert next_hit["triggered"] == 0
    assert next_hit["confirmation_count"] == 1


def test_btc_volatility_monitor_expires_unconfirmed_opportunity() -> None:
    prices = iter([100.0, 101.2, 101.25])
    times = iter([1000.0, 1060.0, 1361.0])
    monitor = BTCVolatilityMonitor(
        quote_fetcher=lambda _symbol: {"price": next(prices)},
        now_provider=lambda: next(times),
    )

    monitor.run_once(_config())
    monitor.run_once(_config(btc_volatility_monitor_max_watch_minutes=5))
    expired = monitor.run_once(_config(btc_volatility_monitor_max_watch_minutes=5))

    assert expired["reason"] == "watch_expired"
    assert expired["triggered"] == 0


def test_btc_volatility_monitor_contains_quote_provider_errors() -> None:
    monitor = BTCVolatilityMonitor(
        quote_fetcher=lambda _symbol: (_ for _ in ()).throw(RuntimeError("all providers unavailable")),
        now_provider=lambda: 1000.0,
    )

    result = monitor.run_once(_config())

    assert result["reason"] == "quote_error"
    assert result["error_type"] == "RuntimeError"
    assert result["triggered"] == 0


def test_parse_window_tiers_sorts_and_drops_invalid_entries() -> None:
    tiers = _parse_window_tiers("5:1.0,1:0.4,3:0.7")

    assert [(tier.window_seconds, tier.threshold_pct) for tier in tiers] == [
        (60, 0.4),
        (180, 0.7),
        (300, 1.0),
    ]
    assert _parse_window_tiers("") == []
    assert _parse_window_tiers(None) == []
    assert _parse_window_tiers("abc,5:xyz,:,2:0.05,0:1.0") == []
    assert [(tier.window_seconds, tier.threshold_pct) for tier in _parse_window_tiers("0.5:0.3")] == [(30, 0.3)]


def test_tier_mode_fast_window_catches_move_legacy_misses() -> None:
    tier_config = _config(btc_volatility_monitor_window_tiers="1:0.4,5:1.0")
    prices = iter([100.0, 100.2, 100.55, 100.8])
    times = iter([1000.0, 1030.0, 1060.0, 1090.0])
    tiered = BTCVolatilityMonitor(
        quote_fetcher=lambda _symbol: {"price": next(prices)},
        now_provider=lambda: next(times),
    )

    warmup = tiered.run_once(tier_config)
    quiet = tiered.run_once(tier_config)
    spike = tiered.run_once(tier_config)
    triggered = tiered.run_once(tier_config)

    assert warmup["reason"] == "warming_up"
    assert quiet["reason"] == "below_threshold"
    assert spike["reason"] == "awaiting_confirmation"
    assert spike["event_detected"] == 1
    assert spike["tier_window_seconds"] == 60
    assert spike["confirmation_required"] == 2
    assert triggered["triggered"] == 1
    assert triggered["reason"] == "entry_signal"
    assert triggered["trade_direction"] == "long"
    assert triggered["tier_window_seconds"] == 60

    legacy_prices = iter([100.0, 100.2, 100.55, 100.8])
    legacy_times = iter([1000.0, 1030.0, 1060.0, 1090.0])
    legacy = BTCVolatilityMonitor(
        quote_fetcher=lambda _symbol: {"price": next(legacy_prices)},
        now_provider=lambda: next(legacy_times),
    )
    legacy.run_once(_config())
    legacy.run_once(_config())
    legacy.run_once(_config())
    legacy_final = legacy.run_once(_config())

    assert legacy_final["triggered"] == 0
    assert legacy_final["reason"] == "early_warning_active"


def test_tier_mode_detects_liquidity_sweep_once_after_spike_fades() -> None:
    tier_config = _config(btc_volatility_monitor_window_tiers="1:0.4,5:1.0")
    prices = iter([100.0, 101.2, 100.3, 100.35])
    times = iter([1000.0, 1010.0, 1020.0, 1030.0])
    monitor = BTCVolatilityMonitor(
        quote_fetcher=lambda _symbol: {"price": next(prices)},
        now_provider=lambda: next(times),
    )

    monitor.run_once(tier_config)
    monitor.run_once(tier_config)
    sweep = monitor.run_once(tier_config)
    follow_up = monitor.run_once(tier_config)

    assert sweep["reason"] == "liquidity_sweep"
    assert sweep["trigger_reason"] == "liquidity_sweep"
    assert sweep["event_detected"] == 1
    assert sweep["triggered"] == 0
    assert sweep["sweep_side"] == "up"
    assert sweep["swept_extreme_price"] == 101.2
    assert sweep["trade_direction"] == "short"
    assert sweep["suggested_trade_action"] == "watch_short_after_sweep"
    assert follow_up["reason"] != "liquidity_sweep"
    assert follow_up["event_detected"] == 0


def test_reversal_bypasses_cooldown_only_when_enabled() -> None:
    prices = [100.0, 101.2, 101.5, 100.6, 99.4, 98.9, 98.6]
    times = [1000.0, 1060.0, 1120.0, 1180.0, 1240.0, 1300.0, 1360.0]

    def _run_sequence(allow_reversal: bool) -> list:
        price_iter = iter(prices)
        time_iter = iter(times)
        monitor = BTCVolatilityMonitor(
            quote_fetcher=lambda _symbol: {"price": next(price_iter)},
            now_provider=lambda: next(time_iter),
        )
        config = _config(btc_volatility_monitor_cooldown_allow_reversal=allow_reversal)
        return [monitor.run_once(config) for _ in prices]

    bypassed = _run_sequence(True)
    assert bypassed[2]["triggered"] == 1
    assert bypassed[2]["trade_direction"] == "long"
    final = bypassed[-1]
    assert final["triggered"] == 1
    assert final["cooldown_bypassed"] == 1
    assert final["trade_direction"] == "short"
    assert final["direction"] == "down"

    blocked = _run_sequence(False)
    assert blocked[2]["triggered"] == 1
    assert blocked[-1]["triggered"] == 0
    assert blocked[-1]["suppressed"] == 1
    assert blocked[-1]["reason"] == "cooldown"


def _run_prices(config, prices, step: float = 60.0) -> list:
    price_iter = iter(prices)
    times = iter([1000.0 + index * step for index in range(len(prices))])
    monitor = BTCVolatilityMonitor(
        quote_fetcher=lambda _symbol: {"price": next(price_iter)},
        now_provider=lambda: next(times),
    )
    return [monitor.run_once(config) for _ in prices]


def test_adaptive_threshold_lowers_threshold_in_quiet_market() -> None:
    quiet = [100.0 if index % 2 == 0 else 100.005 for index in range(40)]
    prices = quiet + [100.1, 100.2, 100.3, 100.4]

    adaptive_on = _run_prices(_config(btc_volatility_monitor_adaptive_threshold_enabled=True), prices)
    adaptive_off = _run_prices(_config(), prices)

    assert adaptive_on[-1]["event_detected"] == 1
    assert adaptive_on[-1]["threshold_pct"] == 0.4  # clamped to the adaptive floor
    assert adaptive_off[-1]["event_detected"] == 0


def test_adaptive_threshold_raises_threshold_in_volatile_market() -> None:
    volatile = [100.0 if index % 2 == 0 else 100.5 for index in range(40)]
    prices = volatile + [100.7, 100.9, 101.1, 101.3]

    adaptive_on = _run_prices(_config(btc_volatility_monitor_adaptive_threshold_enabled=True), prices)
    adaptive_off = _run_prices(_config(), prices)

    assert adaptive_on[-1]["event_detected"] == 0
    assert adaptive_on[-1]["threshold_pct"] == 2.0  # clamped to the adaptive cap
    assert adaptive_off[-1]["event_detected"] == 1


def test_adaptive_threshold_falls_back_to_static_until_enough_samples() -> None:
    prices = [100.0, 100.005, 100.1, 100.2, 100.3, 100.4]
    results = _run_prices(_config(btc_volatility_monitor_adaptive_threshold_enabled=True), prices)

    # only a handful of samples: the static 1.0% threshold still applies
    assert results[-1]["event_detected"] == 0
    assert results[-1]["threshold_pct"] == 1.0


def test_velocity_trigger_escalates_fast_poll_to_poll_move() -> None:
    quiet = [100.0 if index % 2 == 0 else 100.005 for index in range(40)]
    prices = quiet + [100.35]

    velocity_on = _run_prices(_config(btc_volatility_monitor_velocity_enabled=True), prices)
    velocity_off = _run_prices(_config(), prices)

    final = velocity_on[-1]
    assert final["event_detected"] == 1
    assert final["velocity_trigger"] == 1
    assert "fast_path" not in final  # velocity moves keep full confirmation
    assert final["confirmation_required"] == 2
    assert final["direction"] == "up"
    assert velocity_off[-1]["event_detected"] == 0
    assert "velocity_trigger" not in velocity_off[-1]


def test_velocity_trigger_waits_for_minimum_samples() -> None:
    prices = [100.0, 100.005, 100.0, 100.005, 100.35]
    results = _run_prices(_config(btc_volatility_monitor_velocity_enabled=True), prices)

    assert results[-1]["event_detected"] == 0
    assert "velocity_trigger" not in results[-1]


def test_velocity_floor_tracks_fastest_tier_threshold() -> None:
    quiet = [100.0 if index % 2 == 0 else 100.005 for index in range(40)]
    tier_config = _config(
        btc_volatility_monitor_window_tiers="1:0.4",
        btc_volatility_monitor_velocity_enabled=True,
    )

    # +0.145% poll-to-poll jitter passes the static 0.1 floor but stays below
    # the tier-linked floor (0.5 x 0.4 = 0.2): noise must not become an event.
    noise = _run_prices(tier_config, quiet + [100.15])
    assert noise[-1]["event_detected"] == 0
    assert "velocity_trigger" not in noise[-1]

    real = _run_prices(tier_config, quiet + [100.35])
    assert real[-1]["event_detected"] == 1
    assert real[-1]["velocity_trigger"] == 1
    assert real[-1]["confirmation_required"] == 2


def test_velocity_reversal_does_not_bypass_cooldown() -> None:
    def _seed_monitor():
        prices = iter([100.0, 100.0])
        times = iter([1000.0, 1060.0])
        monitor = BTCVolatilityMonitor(
            quote_fetcher=lambda _symbol: {"price": next(prices)},
            now_provider=lambda: next(times),
        )
        config = _config(btc_volatility_monitor_cooldown_allow_reversal=True)
        monitor.run_once(config)
        monitor.run_once(config)
        return monitor, config

    def _seed_reversal_opportunity(monitor: BTCVolatilityMonitor, *, velocity: bool) -> None:
        monitor._last_trigger_at = 1060.0
        monitor._last_trigger_direction = "up"
        monitor._active_opportunity = ActiveOpportunity(
            direction="down",
            detected_at=1105.0,
            baseline_timestamp=1060.0,
            baseline_price=100.0,
            opportunity_price=100.0,
            threshold_pct=1.0,
            initial_change_pct=-0.2,
            velocity_trigger=velocity,
        )
        monitor._confirmation_direction = "down"
        monitor._confirmation_count = 1

    prices = iter([99.6])
    times = iter([1120.0])
    velocity_monitor, config = _seed_monitor()
    velocity_monitor.quote_fetcher = lambda _symbol: {"price": next(prices)}
    velocity_monitor.now_provider = lambda: next(times)
    _seed_reversal_opportunity(velocity_monitor, velocity=True)

    suppressed = velocity_monitor.run_once(config)
    assert suppressed["triggered"] == 0
    assert suppressed["suppressed"] == 1
    assert suppressed["reason"] == "cooldown"

    prices = iter([99.6])
    times = iter([1120.0])
    breach_monitor, config = _seed_monitor()
    breach_monitor.quote_fetcher = lambda _symbol: {"price": next(prices)}
    breach_monitor.now_provider = lambda: next(times)
    _seed_reversal_opportunity(breach_monitor, velocity=False)

    bypassed = breach_monitor.run_once(config)
    assert bypassed["triggered"] == 1
    assert bypassed["cooldown_bypassed"] == 1
    assert bypassed["trade_direction"] == "short"


def test_fast_confirmation_reduces_required_samples_for_violent_moves() -> None:
    prices = [100.0, 101.6, 102.0, 102.1]
    fast_on = _run_prices(
        _config(
            btc_volatility_monitor_confirmation_samples=3,
            btc_volatility_monitor_fast_confirmation_enabled=True,
        ),
        prices,
    )

    assert fast_on[1]["event_detected"] == 1
    assert fast_on[1]["fast_path"] == 1
    assert fast_on[2]["triggered"] == 1
    assert fast_on[2]["confirmation_required"] == 1

    fast_off = _run_prices(_config(btc_volatility_monitor_confirmation_samples=3), prices)
    assert fast_off[1]["fast_path"] == 1  # marked, but behaviour unchanged while disabled
    assert fast_off[2]["triggered"] == 0
    assert fast_off[2]["reason"] == "watching_opportunity"
    assert fast_off[2]["confirmation_required"] == 3
    assert fast_off[3]["triggered"] == 1


def test_fast_confirmation_still_requires_price_to_cross_entry_confirmation() -> None:
    prices = [100.0, 101.6, 101.7, 102.0]
    results = _run_prices(
        _config(
            btc_volatility_monitor_fast_confirmation_enabled=True,
            btc_volatility_monitor_confirmation_samples=3,
        ),
        prices,
    )

    assert results[1]["fast_path"] == 1
    assert results[2]["triggered"] == 0
    assert results[2]["reason"] == "watching_opportunity"
    assert results[2]["entry_executable_now"] == 0
    assert results[3]["triggered"] == 1
    assert results[3]["entry_executable_now"] == 1
    assert results[3]["impulse_stage"] == "early_continuation"


def test_late_extension_triggers_analysis_but_marks_entry_not_executable() -> None:
    results = _run_prices(_config(), [100.0, 101.2, 102.3])

    triggered = results[-1]
    assert triggered["triggered"] == 1
    assert triggered["entry_executable_now"] == 0
    assert triggered["impulse_stage"] == "late_extension"
    assert triggered["entry_overshoot_pct"] > triggered["max_entry_overshoot_pct"]


def test_impulse_exhaustion_cancels_candidate_before_analysis() -> None:
    results = _run_prices(
        _config(btc_volatility_monitor_confirmation_samples=3),
        [100.0, 101.2, 101.7, 101.3],
    )

    exhausted = results[-1]
    assert exhausted["triggered"] == 0
    assert exhausted["event_detected"] == 1
    assert exhausted["reason"] == "impulse_exhausted"
    assert exhausted["impulse_stage"] == "exhaustion_candidate"
    assert exhausted["entry_executable_now"] == 0
