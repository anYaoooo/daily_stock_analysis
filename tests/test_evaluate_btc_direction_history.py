from datetime import datetime, timedelta, timezone

import pandas as pd

from scripts.evaluate_btc_direction_history import (
    _close_direction_score,
    _direction_score,
    evaluate_history,
)


def test_direction_score_uses_mfe_greater_than_mae() -> None:
    future = pd.DataFrame({"high": [105.0, 106.0], "low": [99.0, 100.0]})

    assert _direction_score("long", 100.0, future) is True
    assert _direction_score("short", 100.0, future) is False


def test_close_direction_score_requires_positive_realized_directional_return() -> None:
    assert _close_direction_score("long", 100.0, 99.0) is False
    assert _close_direction_score("short", 100.0, 99.0) is True


def test_evaluate_history_returns_model_and_baseline_metrics() -> None:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(100):
        close = 100.0 + index * 0.2
        rows.append(
            {
                "date": start + timedelta(hours=index),
                "open": close - 0.05,
                "high": close + 0.1,
                "low": close - 0.1,
                "close": close,
                "volume": 1000.0,
            }
        )

    result = evaluate_history(pd.DataFrame(rows), horizon_bars=12, lookback_bars=60, step=5)

    assert result["input_rows"] == 100
    assert result["deterministic_vote"]["evaluated_signals"] > 0
    assert set(result["baselines"]) == {"always_long", "previous_bar_direction", "random_50_50"}
    assert "close_directional_accuracy" in result["deterministic_vote"]
    assert "max_drawdown_pct" in result["deterministic_vote"]


def test_evaluate_history_reports_multiple_horizons_and_net_return() -> None:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(120):
        close = 100.0 + index * 0.3
        rows.append(
            {
                "date": start + timedelta(hours=index),
                "open": close + 0.05,
                "high": close + 0.2,
                "low": close - 0.1,
                "close": close,
                "volume": 1000.0,
            }
        )

    result = evaluate_history(
        pd.DataFrame(rows),
        horizon_bars=4,
        horizons=[1, 4, 12],
        lookback_bars=60,
        step=5,
        fee_bps=5.0,
        slippage_bps=2.0,
    )

    assert set(result["horizon_evaluations"]) == {"1", "4", "12"}
    four_hour = result["horizon_evaluations"]["4"]
    assert four_hour["trading_costs"]["round_trip_cost_bps"] == 14.0
    assert four_hour["deterministic_vote"]["coverage"] is not None
    assert "avg_net_return_pct" in four_hour["deterministic_vote"]
    assert "by_direction" in four_hour["deterministic_vote"]
    assert "by_strength" in four_hour["deterministic_vote"]


def test_evaluate_history_accepts_custom_direction_threshold() -> None:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(100):
        close = 100.0 + index * 0.2
        rows.append(
            {
                "date": start + timedelta(hours=index),
                "open": close,
                "high": close + 0.1,
                "low": close - 0.1,
                "close": close,
                "volume": 1000.0,
            }
        )

    result = evaluate_history(
        pd.DataFrame(rows),
        horizon_bars=12,
        lookback_bars=60,
        step=5,
        direction_threshold=0.7,
    )

    assert result["direction_threshold"] == 0.7
    assert result["deterministic_vote"]["direction_threshold"] == 0.7


def test_primary_horizon_is_kept_when_multiple_horizons_omit_it() -> None:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(90):
        close = 100.0 + index * 0.2
        rows.append(
            {
                "date": start + timedelta(hours=index),
                "open": close,
                "high": close + 0.1,
                "low": close - 0.1,
                "close": close,
                "volume": 1000.0,
            }
        )

    result = evaluate_history(pd.DataFrame(rows), horizon_bars=24, horizons=[1, 4], step=10)

    assert set(result["horizon_evaluations"]) == {"1", "4", "24"}
    assert result["horizon_bars"] == 24
