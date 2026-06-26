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
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_btc_volatility_monitor_triggers_when_window_move_exceeds_threshold() -> None:
    prices = iter([100.0, 101.2])
    times = iter([1000.0, 1060.0])
    monitor = BTCVolatilityMonitor(
        quote_fetcher=lambda _symbol: {"price": next(prices), "provider_timestamp": "quote-ts"},
        now_provider=lambda: next(times),
    )

    warmup = monitor.run_once(_config())
    triggered = monitor.run_once(_config())

    assert warmup["reason"] == "warming_up"
    assert triggered["triggered"] == 1
    assert triggered["reason"] == "volatility_threshold"
    assert triggered["direction"] == "up"
    assert triggered["change_pct"] == 1.2
    assert triggered["baseline_price"] == 100.0
    assert triggered["price"] == 101.2


def test_btc_volatility_monitor_suppresses_retrigger_during_cooldown() -> None:
    prices = iter([100.0, 101.2, 102.5])
    times = iter([1000.0, 1060.0, 1120.0])
    monitor = BTCVolatilityMonitor(
        quote_fetcher=lambda _symbol: {"price": next(prices)},
        now_provider=lambda: next(times),
    )

    monitor.run_once(_config())
    first = monitor.run_once(_config())
    second = monitor.run_once(_config())

    assert first["triggered"] == 1
    assert second["triggered"] == 0
    assert second["suppressed"] == 1
    assert second["reason"] == "cooldown"


def test_btc_volatility_monitor_prunes_old_baseline() -> None:
    prices = iter([100.0, 100.2, 101.4])
    times = iter([1000.0, 1400.0, 1460.0])
    monitor = BTCVolatilityMonitor(
        quote_fetcher=lambda _symbol: {"price": next(prices)},
        now_provider=lambda: next(times),
    )

    monitor.run_once(_config(btc_volatility_monitor_window_minutes=5))
    warmup_after_prune = monitor.run_once(_config(btc_volatility_monitor_window_minutes=5))
    triggered = monitor.run_once(_config(btc_volatility_monitor_window_minutes=5))

    assert warmup_after_prune["reason"] == "warming_up"
    assert triggered["triggered"] == 1
    assert triggered["baseline_price"] == 100.2
