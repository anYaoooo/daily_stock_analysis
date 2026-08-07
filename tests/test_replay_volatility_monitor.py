# -*- coding: utf-8 -*-
"""Offline tests for scripts/replay_volatility_monitor.py (deterministic, no network)."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import List

from scripts.replay_volatility_monitor import (
    Kline,
    _build_config,
    _compute_metrics,
    _load_klines_csv,
    _price_reached,
    _run_replay,
    _sample_points,
    _scan_true_events,
)

_BASE_TS = 1_754_000_000.0


def _kline(index: int, open_p: float, high_p: float, low_p: float, close_p: float) -> Kline:
    return Kline(ts=_BASE_TS + index * 60.0, open=open_p, high=high_p, low=low_p, close=close_p)


def _flat(count: int, price: float, start: int = 0) -> List[Kline]:
    return [_kline(start + i, price, price * 1.0002, price * 0.9998, price) for i in range(count)]


def _args(**overrides) -> argparse.Namespace:
    defaults = dict(
        window_minutes=5,
        threshold_pct=1.0,
        early_warning_pct=0.3,
        spike_revert_pct=0.4,
        confirmation_samples=2,
        entry_confirmation_pct=0.2,
        invalidation_pct=0.5,
        max_watch_minutes=20,
        cooldown_minutes=30,
        cooldown_allow_reversal=False,
        adaptive_threshold=False,
        adaptive_k=2.5,
        adaptive_min_pct=0.4,
        adaptive_max_pct=2.0,
        adaptive_lookback_minutes=240,
        velocity=False,
        velocity_mult=3.0,
        velocity_min_pct=0.1,
        fast_confirmation=False,
        fast_confirmation_mult=1.5,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _slow_ramp_klines() -> List[Kline]:
    """30 flat bars, then a 15-bar +1.8% ramp (0.12%/bar), then flat."""
    klines = _flat(30, 100.0)
    price = 100.0
    for _ in range(15):
        close = price * 1.0012
        klines.append(_kline(len(klines), price, close * 1.0001, price * 0.9999, close))
        price = close
    klines.extend(_flat(30, price, start=len(klines)))
    return klines


def test_scan_true_events_labels_ramp_once() -> None:
    events = _scan_true_events(_slow_ramp_klines(), window_minutes=15, min_pct=1.0)
    assert len(events) == 1
    event = events[0]
    assert event.direction == "up"
    assert 1.5 <= event.magnitude_pct <= 2.1
    assert event.extreme_ts > event.start_ts


def test_scan_true_events_ignores_quiet_market() -> None:
    assert _scan_true_events(_flat(60, 100.0), window_minutes=15, min_pct=1.0) == []


def test_tiers_replay_catches_slow_move_that_legacy_misses() -> None:
    klines = _slow_ramp_klines()
    points = _sample_points(klines, 60)
    args = _args()
    tiers_result = _run_replay("tiers", points, _build_config(args, "1:0.4,3:0.7,5:1.0,15:1.5"))
    legacy_result = _run_replay("legacy", points, _build_config(args, ""))

    assert tiers_result.detections, "tiers mode should detect the 15-minute ramp"
    assert all(d.get("direction") == "up" for d in tiers_result.detections)
    assert not legacy_result.detections, "legacy 5m/1.0% window must stay blind to the slow ramp"

    events = _scan_true_events(klines, window_minutes=15, min_pct=1.0)
    metrics = _compute_metrics(events, tiers_result, klines, false_positive_minutes=30)
    assert metrics["detection_rate"] == 1.0
    assert metrics["avg_detection_delay_seconds"] is not None


def test_subminute_sampling_lets_sweep_fire() -> None:
    # slow drift up (+0.07%/bar), then a fast wick that fully reverts inside one
    # bar: no tier window ever breaches, but the window extreme ran up >=0.4%
    # versus the oldest snapshot and faded >=0.4% -> liquidity sweep alert.
    klines: List[Kline] = []
    price = 100.0
    for i in range(10):
        close = price * 1.0007
        klines.append(_kline(i, price, close * 1.0001, price * 0.9999, close))
        price = close
    spike_open = price  # ~100.7 after the drift
    klines.append(_kline(len(klines), spike_open, spike_open * 1.006, spike_open * 0.9955, spike_open * 0.996))
    klines.extend(_flat(30, spike_open * 0.996, start=len(klines)))

    coarse = _run_replay("tiers-60s", _sample_points(klines, 60), _build_config(_args(), "1:0.4,3:0.7,5:1.0"))
    fine = _run_replay("tiers-15s", _sample_points(klines, 15), _build_config(_args(), "1:0.4,3:0.7,5:1.0"))

    assert not coarse.sweeps, "60s close-only sampling cannot see the intra-minute wick"
    assert len(fine.sweeps) == 1, "sub-minute sampling should surface the faded wick as a liquidity sweep"
    sweep = fine.sweeps[0]
    assert sweep["trigger_reason"] == "liquidity_sweep"
    assert sweep["sweep_side"] == "up"
    assert all(not d.get("triggered") for d in fine.detections), "sweeps must stay alert-only"


def test_price_reached_handles_boundaries() -> None:
    klines = _flat(12, 100.0)
    klines[5] = _kline(5, 100.0, 101.0, 99.0, 100.0)
    start = klines[0].ts
    assert _price_reached(klines, after_ts=start, minutes=10, direction="up", target_price=100.5) is True
    assert _price_reached(klines, after_ts=start, minutes=10, direction="down", target_price=99.5) is True
    assert _price_reached(klines, after_ts=start, minutes=10, direction="up", target_price=101.5) is False
    # window never reaches the target and the data ends before the deadline -> unjudgable
    assert _price_reached(klines, after_ts=start, minutes=60, direction="up", target_price=101.5) is None


def test_load_klines_csv_parses_common_timestamp_formats(tmp_path: Path) -> None:
    csv_path = tmp_path / "klines.csv"
    csv_path.write_text(
        "timestamp,open,high,low,close\n"
        f"{int(_BASE_TS) * 1000},100,101,99,100.5\n"
        "2025-08-01 01:01:00,100.5,101.5,100,101\n",
        encoding="utf-8",
    )
    klines = _load_klines_csv(csv_path)
    assert len(klines) == 2
    assert klines[0].ts == _BASE_TS
    assert klines[0].close == 100.5
    assert klines[1].ts > klines[0].ts  # ISO timestamps parse and keep ordering
