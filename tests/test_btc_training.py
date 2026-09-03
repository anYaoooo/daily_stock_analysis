# -*- coding: utf-8 -*-
"""Regression tests for the leakage-safe BTC multi-task baseline."""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.services.btc_training import (
    BtcTrainingConfig,
    BtcTrainingService,
    SUPPORTED_MODELS,
    _execution_evaluation,
    build_feature_frame,
    fixed_holdout_split,
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
    bars = _bars(120)
    frame = build_feature_frame(
        bars,
        config=BtcTrainingConfig(horizons={"1h": 1, "4h": 4}, lookback_bars=20),
    )
    assert frame["feature_log_return_1"].iloc[40] == pytest.approx(
        np.log(frame["reference_close"].iloc[40] / frame["reference_close"].iloc[39])
    )
    assert frame["target_return_1h"].iloc[40] == pytest.approx(
        np.log(frame["reference_close"].iloc[41] / frame["reference_close"].iloc[40])
    )
    assert frame["target_trade_return_1h"].iloc[40] == pytest.approx(
        np.log(frame["reference_close"].iloc[41] / bars["open"].iloc[41])
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


def test_fixed_holdout_split_keeps_tail_and_purge_out_of_training() -> None:
    split = fixed_holdout_split(500, min_train_bars=200, holdout_bars=100, purge_bars=24)
    assert split is not None
    assert split["train_start"] == 0
    assert split["train_end"] == 376
    assert split["purge_start"] == 376
    assert split["purge_end"] == 400
    assert split["holdout_start"] == 400
    assert split["holdout_end"] == 500
    assert split["train_end"] + split["purge_bars"] == split["holdout_start"]
    assert fixed_holdout_split(399, min_train_bars=300, holdout_bars=100, purge_bars=24) is None


def test_service_returns_distribution_volatility_and_regime() -> None:
    config = BtcTrainingConfig(
        horizons={"1h": 1, "4h": 4},
        min_train_bars=120,
        validation_bars=24,
        folds=3,
        holdout_bars=96,
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
    execution = result["evaluations"]["4h"]["execution_evaluation"]
    assert execution["non_overlapping"] is True
    assert execution["decision_stride_bars"] == 4
    assert execution["round_trip_cost_bps"] == 14.0
    assert "directional_accuracy" in execution
    assert execution["decision_count"] <= result["evaluations"]["4h"]["samples"]
    holdout = result["evaluations"]["4h"]["holdout_evaluation"]
    assert holdout["data_quality"] == "available"
    assert holdout["evaluation_type"] == "fixed_tail_holdout"
    assert holdout["holdout_samples"] == 96
    assert holdout["split"]["train_end"] + holdout["purge_bars"] == holdout["split"]["holdout_start"]
    assert holdout["model_fit_scope"] == "rows_before_holdout_purge"
    assert holdout["cost_sensitivity"]["same_holdout_predictions"] is True
    assert holdout["cost_sensitivity"]["refit_per_cost"] is False
    assert [item["round_trip_cost_bps"] for item in holdout["cost_sensitivity"]["scenarios"]] == [14.0, 30.0, 50.0]
    assert result["forecasts"]["4h"]["training_data_scope"] == "before_holdout_purge"
    assert result["forecasts"]["4h"]["training_samples"] == holdout["train_samples"]


def test_training_config_exposes_model_and_execution_options() -> None:
    assert SUPPORTED_MODELS == ("linear", "lightgbm")
    config = BtcTrainingConfig(
        model="lightgbm",
        fee_bps_per_side=6,
        slippage_bps_per_side=3,
        decision_stride=7,
        holdout_bars=72,
        cost_sensitivity_bps=(50, 14, 30, 14),
    )
    assert config.model == "lightgbm"
    assert config.round_trip_cost_bps == 18.0
    assert config.decision_stride == 7
    assert config.holdout_bars == 72
    assert config.cost_sensitivity_bps == (14.0, 30.0, 50.0)


def test_execution_evaluation_drops_overlapping_decisions_and_applies_cost() -> None:
    config = BtcTrainingConfig(
        horizons={"4h": 4},
        fee_bps_per_side=5,
        slippage_bps_per_side=2,
    )
    result = _execution_evaluation(
        [
            {
                "decision_index": 10,
                "predicted_direction": 1,
                "predicted_return": 0.01,
                "actual_trade_return": float(np.log(1.01)),
                "actual_trade_direction": 1,
            },
            {
                "decision_index": 11,
                "predicted_direction": -1,
                "predicted_return": -0.01,
                "actual_trade_return": float(np.log(0.99)),
                "actual_trade_direction": -1,
            },
            {
                "decision_index": 14,
                "predicted_direction": -1,
                "predicted_return": -0.01,
                "actual_trade_return": float(np.log(0.99)),
                "actual_trade_direction": -1,
            },
        ],
        horizon=4,
        config=config,
    )
    assert result["decision_count"] == 2
    assert result["signal_count"] == 2
    assert result["directional_accuracy"] == 1.0
    assert result["avg_net_return_pct"] == pytest.approx(0.865051, abs=0.000001)
    assert result["round_trip_cost_bps"] == 14.0


def test_lightgbm_model_runs_through_the_same_walk_forward_contract() -> None:
    pytest.importorskip("lightgbm")
    config = BtcTrainingConfig(
        horizons={"4h": 4},
        min_train_bars=120,
        validation_bars=24,
        folds=2,
        model="lightgbm",
        holdout_bars=96,
    )
    result = BtcTrainingService(config).build(_bars(420))
    assert result["model"] == "lightgbm"
    assert result["forecasts"]["4h"]["model"] == "lightgbm"
    assert result["evaluations"]["4h"]["execution_evaluation"]["non_overlapping"] is True
    assert result["evaluations"]["4h"]["holdout_evaluation"]["data_quality"] == "available"


def test_service_reports_insufficient_data_without_forecast() -> None:
    result = BtcTrainingService(BtcTrainingConfig(min_train_bars=336, folds=3)).build(_bars(80))
    assert result["data_quality"] == "insufficient"
    assert result["forecasts"]["1h"] is None
