# -*- coding: utf-8 -*-
"""Regression tests for the leakage-safe BTC multi-task baseline."""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.services.btc_training import (
    BtcTrainingConfig,
    BtcTrainingService,
    build_feature_frame,
    walk_forward_splits,
)


def _bars(count: int = 520) -> pd.DataFrame:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    close = 90_000.0
    rows = []
    for index in range(count):
        change = 0.001 * np.sin(index / 8) + 0.0003 * np.cos(index / 3)
        open_price = close
        close *= 1 + change
        rows.append(
            {
                "date": start + timedelta(hours=index),
                "open": open_price,
                "high": max(open_price, close) * 1.001,
                "low": min(open_price, close) * 0.999,
                "close": close,
                "volume": 1000 + index % 20 * 10,
            }
        )
    frame = pd.DataFrame(rows)
    frame.attrs["fetched_at"] = (start + timedelta(hours=count + 1)).isoformat()
    return frame


def test_feature_frame_separates_causal_features_and_future_targets() -> None:
    frame = build_feature_frame(
        _bars(120),
        config=BtcTrainingConfig(horizons={"1h": 1, "4h": 4}, lookback_bars=20),
    )
    assert frame["feature_log_return_1"].iloc[40] == pytest.approx(
        np.log(frame["reference_close"].iloc[40] / frame["reference_close"].iloc[39])
    )
    assert frame["target_return_1h"].iloc[40] == pytest.approx(
        np.log(frame["reference_close"].iloc[41] / frame["reference_close"].iloc[40])
    )
    assert all(column.startswith("feature_") or column.startswith("target_") or column in {"date", "reference_close"} for column in frame)


def test_feature_frame_excludes_open_bar_using_fetch_timestamp() -> None:
    bars = _bars(80)
    last_date = pd.Timestamp(bars.iloc[-1]["date"])
    bars.attrs["fetched_at"] = (last_date + pd.Timedelta(minutes=30)).isoformat()
    frame = build_feature_frame(bars, config=BtcTrainingConfig(lookback_bars=20))
    assert len(frame) == len(bars) - 1
    assert pd.Timestamp(frame.iloc[-1]["date"]) == last_date - pd.Timedelta(hours=1)


def test_walk_forward_splits_have_purge_gap() -> None:
    splits = walk_forward_splits(1000, min_train_bars=300, validation_bars=50, folds=3, purge_bars=24)
    assert len(splits) == 3
    assert all(item["train_end"] + item["purge_bars"] <= item["validation_start"] for item in splits)
    assert all(item["train_start"] == 0 for item in splits)


def test_service_returns_distribution_volatility_and_regime() -> None:
    config = BtcTrainingConfig(
        horizons={"1h": 1, "4h": 4},
        min_train_bars=120,
        validation_bars=24,
        folds=3,
    )
    result = BtcTrainingService(config).build(_bars(420))
    assert result["mode"] == "offline_research"
    assert result["participates_in_decision"] is False
    assert result["leakage_guard"]["random_split"] is False
    assert set(result["forecasts"]) == {"1h", "4h"}
    forecast = result["forecasts"]["4h"]
    assert forecast is not None
    assert set(forecast["direction_probabilities"]) == {"down", "neutral", "up"}
    assert sum(forecast["direction_probabilities"].values()) == pytest.approx(1.0)
    assert set(forecast["return_quantiles"]) == {"0.1", "0.25", "0.5", "0.75", "0.9"}
    assert forecast["regime"] in {"trend_up", "trend_down", "high_volatility", "sideways"}
    assert result["evaluations"]["4h"]["fold_count"] == 3
    assert "quantile_pinball_loss" in result["evaluations"]["4h"]


def test_service_reports_insufficient_data_without_forecast() -> None:
    result = BtcTrainingService(BtcTrainingConfig(min_train_bars=336, folds=3)).build(_bars(80))
    assert result["data_quality"] == "insufficient"
    assert result["forecasts"]["1h"] is None
