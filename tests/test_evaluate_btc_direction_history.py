from datetime import datetime, timedelta, timezone

import pandas as pd

from scripts.evaluate_btc_direction_history import _direction_score, evaluate_history


def test_direction_score_uses_mfe_greater_than_mae() -> None:
    future = pd.DataFrame({"high": [105.0, 106.0], "low": [99.0, 100.0]})

    assert _direction_score("long", 100.0, future) is True
    assert _direction_score("short", 100.0, future) is False


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
