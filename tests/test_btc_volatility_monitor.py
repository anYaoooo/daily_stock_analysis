# -*- coding: utf-8 -*-
"""Tests for BTC volatility-triggered analysis monitor."""

from __future__ import annotations

from types import SimpleNamespace

from src.services.btc_volatility_monitor import BTCVolatilityMonitor


def _config(**overrides):
    defaults = {
        "btc_volatility_monitor_enabled": True,
        "btc_volatility_monitor_symbol": "BTC",
        "btc_volatility_monitor_window_minutes": 5,
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
    assert awaiting_confirmation["confirmation_count"] == 1
    assert awaiting_confirmation["confirmation_required"] == 2
    assert triggered["triggered"] == 1
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
