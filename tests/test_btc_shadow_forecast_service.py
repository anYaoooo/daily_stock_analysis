# -*- coding: utf-8 -*-
"""Tests for the leakage-safe BTC hourly shadow forecast."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from unittest.mock import patch

import numpy as np
import pandas as pd

from src.config import Config
from src.services.btc_shadow_forecast_service import BtcShadowForecastService


def _hourly_bars(count: int = 650) -> pd.DataFrame:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    close = 90000.0
    for index in range(count):
        change = 0.0012 * np.sin(index / 9.0) + 0.0004 * np.cos(index / 3.0)
        open_price = close
        close = open_price * (1.0 + change)
        rows.append(
            {
                "date": start + timedelta(hours=index),
                "open": open_price,
                "high": max(open_price, close) * 1.001,
                "low": min(open_price, close) * 0.999,
                "close": close,
                "volume": 1000 + (index % 29) * 11,
            }
        )
    frame = pd.DataFrame(rows)
    frame.attrs["fetched_at"] = (start + timedelta(hours=count + 1)).isoformat()
    frame.attrs["period"] = "hourly"
    return frame


def test_shadow_forecast_uses_expanding_walk_forward_and_train_only_scaling() -> None:
    result = BtcShadowForecastService(
        min_train_bars=336,
        folds=4,
        validation_bars=24,
    ).build(_hourly_bars())

    assert result["mode"] == "shadow"
    assert result["participates_in_decision"] is False
    assert result["data_quality"] == "available"
    assert result["target"] == "next_closed_1h_return"
    assert result["forecast"]["predicted_direction"] in {"up", "down"}
    assert 0.0 <= result["forecast"]["up_probability"] <= 1.0
    walk_forward = result["walk_forward"]
    assert walk_forward["scheme"] == "expanding_walk_forward"
    assert walk_forward["fold_count"] == 4
    assert walk_forward["out_of_fold_samples"] == 96
    for fold in walk_forward["folds"]:
        assert fold["scaler_fit_scope"] == "train_only"
        assert pd.Timestamp(fold["train_end_at"]) < pd.Timestamp(fold["validation_start_at"])


def test_shadow_forecast_excludes_current_open_hour() -> None:
    bars = _hourly_bars()
    last_index = bars.index[-1]
    bars.loc[last_index, "close"] = 1.0
    bars.attrs["fetched_at"] = (pd.Timestamp(bars.loc[last_index, "date"]) + pd.Timedelta(minutes=5)).isoformat()

    result = BtcShadowForecastService(
        min_train_bars=336,
        folds=2,
        validation_bars=24,
    ).build(bars)

    assert result["data_quality"] == "available"
    assert result["source_closed_bar_count"] == len(bars) - 1


def test_shadow_forecast_reports_insufficient_data_without_fallback_signal() -> None:
    result = BtcShadowForecastService(min_train_bars=336, folds=5, validation_bars=24).build(
        _hourly_bars(120)
    )

    assert result["data_quality"] == "insufficient"
    assert result["forecast"] is None
    assert result["walk_forward"] is None


def test_shadow_forecast_config_can_be_disabled_and_tuned() -> None:
    env = {
        "BTC_SHADOW_FORECAST_ENABLED": "false",
        "BTC_SHADOW_FORECAST_LOOKBACK_DAYS": "45",
        "BTC_SHADOW_FORECAST_MIN_TRAIN_BARS": "360",
        "BTC_SHADOW_FORECAST_FOLDS": "3",
        "BTC_SHADOW_FORECAST_VALIDATION_BARS": "12",
    }
    with patch.dict(os.environ, env, clear=True):
        config = Config._load_from_env()

    assert config.btc_shadow_forecast_enabled is False
    assert config.btc_shadow_forecast_lookback_days == 45
    assert config.btc_shadow_forecast_min_train_bars == 360
    assert config.btc_shadow_forecast_folds == 3
    assert config.btc_shadow_forecast_validation_bars == 12
