# -*- coding: utf-8 -*-
"""Tests for the leakage-safe BTC hourly shadow forecast."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

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
        confidence_threshold=0.5,
    ).build(_hourly_bars())

    assert result["mode"] == "shadow"
    assert result["participates_in_decision"] is False
    assert result["primary_model"] == "walk_forward_calibrated_candidate_selection"
    assert result["data_quality"] == "available"
    assert result["target"] == "next_closed_1h_return"
    assert result["forecast"]["predicted_direction"] in {"up", "down"}
    assert 0.0 <= result["forecast"]["up_probability"] <= 1.0
    primary = result["primary_forecast"]
    assert primary["horizon_hours"] == 4
    assert primary["target"] == "cost_aware_up_down_no_signal"
    assert primary["predicted_action"] in {"up", "down", "no_signal"}
    assert primary["selected_model"] in {"logistic", "hist_gradient_boosting", "lightgbm", "ensemble"}
    assert set(primary["available_models"]).issubset(
        {"logistic", "hist_gradient_boosting", "lightgbm"}
    )
    assert "lightgbm" in primary["available_models"] or "lightgbm" in primary["unavailable_models"]
    if primary["selected_model"] == "ensemble":
        assert primary["ensemble_models"] == primary["available_models"]
    assert primary["participates_in_decision"] is False
    assert primary["up_probability"] + primary["down_probability"] + primary[
        "no_signal_probability"
    ] == pytest.approx(1.0, abs=0.0002)
    curve = result["curve"]
    assert curve["model"] == "direct_multi_horizon_ridge_logistic"
    assert curve["horizon_hours"] == 24
    assert len(curve["points"]) == 24
    assert [point["offset_hours"] for point in curve["points"]] == list(range(1, 25))
    assert all(point["training_bars"] >= 336 for point in curve["points"])
    walk_forward = result["walk_forward"]
    assert walk_forward["scheme"] == "expanding_walk_forward"
    assert walk_forward["origin_selection"] == "evenly_spaced_historical"
    assert walk_forward["fold_count"] == 4
    assert walk_forward["out_of_fold_samples"] == 96
    for fold in walk_forward["folds"]:
        assert fold["scaler_fit_scope"] == "train_only"
        assert pd.Timestamp(fold["train_end_at"]) < pd.Timestamp(fold["validation_start_at"])
    assert set(result["horizon_evaluation"]) == {"1h", "4h", "12h", "24h"}
    primary_evaluation = result["primary_evaluation"]
    assert primary_evaluation["scheme"] == (
        "purged_expanding_walk_forward_with_inner_model_selection"
    )
    assert primary_evaluation["eligible_for_promotion"] is False
    assert primary_evaluation["out_of_fold_samples"] == 96
    assert all(
        fold["purged_horizon_bars"] == 4
        and fold["inner_purged_horizon_bars"] == 4
        and fold["model_selection_scope"] == "train_inner_tail_only"
        and pd.Timestamp(fold["validation_start_at"])
        - pd.Timestamp(fold["train_end_at"])
        > pd.Timedelta(hours=4)
        for fold in primary_evaluation["folds"]
    )
    assert "baselines" in walk_forward
    assert "high_confidence" in walk_forward["confidence_slices"]
    high_confidence = walk_forward["confidence_slices"]["high_confidence"]
    assert high_confidence["round_trip_cost_bps"] == 14.0
    assert "net_mean_return_pct_after_cost" in high_confidence


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
    assert result["primary_forecast"] is None
    assert result["walk_forward"] is None


def test_shadow_forecast_config_can_be_disabled_and_tuned() -> None:
    env = {
        "BTC_SHADOW_FORECAST_ENABLED": "false",
        "BTC_SHADOW_FORECAST_LOOKBACK_DAYS": "45",
        "BTC_SHADOW_FORECAST_MIN_TRAIN_BARS": "360",
        "BTC_SHADOW_FORECAST_FOLDS": "3",
        "BTC_SHADOW_FORECAST_VALIDATION_BARS": "12",
        "BTC_SHADOW_FORECAST_CURVE_HORIZON_HOURS": "48",
        "BTC_SHADOW_FORECAST_PRIMARY_HORIZON_HOURS": "6",
        "BTC_SHADOW_FORECAST_CONFIDENCE_THRESHOLD": "0.62",
    }
    with patch.dict(os.environ, env, clear=True):
        config = Config._load_from_env()

    assert config.btc_shadow_forecast_enabled is False
    assert config.btc_shadow_forecast_lookback_days == 45
    assert config.btc_shadow_forecast_min_train_bars == 360
    assert config.btc_shadow_forecast_folds == 3
    assert config.btc_shadow_forecast_validation_bars == 12
    assert config.btc_shadow_forecast_curve_horizon_hours == 48
    assert config.btc_shadow_forecast_primary_horizon_hours == 6
    assert config.btc_shadow_forecast_confidence_threshold == 0.62


def test_shadow_forecast_model_candidates_and_ensemble_config() -> None:
    env = {
        "BTC_SHADOW_FORECAST_MODEL_CANDIDATES": "logistic,lightgbm,unsupported,logistic",
        "BTC_SHADOW_FORECAST_ENSEMBLE_ENABLED": "false",
    }
    with patch.dict(os.environ, env, clear=True):
        config = Config._load_from_env()

    assert config.btc_shadow_forecast_model_candidates == "logistic,lightgbm,unsupported,logistic"
    assert config.btc_shadow_forecast_ensemble_enabled is False

    service = BtcShadowForecastService(
        model_candidates=config.btc_shadow_forecast_model_candidates,
        ensemble_enabled=config.btc_shadow_forecast_ensemble_enabled,
    )
    assert service.model_candidates == ("logistic", "lightgbm")
    assert service.ensemble_enabled is False


def test_shadow_forecast_ensemble_probabilities_are_normalized() -> None:
    service = BtcShadowForecastService(model_candidates=("logistic", "hist_gradient_boosting"))
    labels = np.array([-1, 0, 1, 1, -1, 0], dtype=int)
    x_train = np.arange(24, dtype=float).reshape(6, 4)
    x_predict = np.arange(8, dtype=float).reshape(2, 4)

    with patch.object(
        service,
        "_raw_trade_probabilities",
        side_effect=[
            np.array([[0.6, 0.2, 0.2], [0.2, 0.3, 0.5]]),
            np.array([[0.2, 0.3, 0.5], [0.4, 0.2, 0.4]]),
        ],
    ):
        probabilities = service._fit_trade_probabilities(
            "ensemble",
            x_train,
            labels,
            x_predict,
            1.0,
            ("logistic", "hist_gradient_boosting"),
        )

    assert probabilities.shape == (2, 3)
    assert probabilities.sum(axis=1) == pytest.approx(np.ones(2))
    assert probabilities[0] == pytest.approx([0.4, 0.25, 0.35])
    assert probabilities[1] == pytest.approx([0.3, 0.25, 0.45])


def test_shadow_forecast_falls_back_when_lightgbm_is_unavailable() -> None:
    service = BtcShadowForecastService(model_candidates="logistic,lightgbm")
    with patch.dict("sys.modules", {"lightgbm": None}):
        available, unavailable = service._available_trade_models()

    assert available == ["logistic"]
    assert unavailable == {"lightgbm": "optional_dependency_missing"}
