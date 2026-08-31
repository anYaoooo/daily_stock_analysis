# -*- coding: utf-8 -*-
"""Deterministic contracts for BTC Transformer online holdout validation."""

import pandas as pd
import pytest

from scripts.validate_btc_transformer_online import (  # noqa: E402
    _realized_targets,
    _score_forecast,
    build_arg_parser,
)


def _bars() -> pd.DataFrame:
    dates = pd.date_range("2026-01-01", periods=25, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "date": dates,
            "close": [100.0 + index for index in range(len(dates))],
        }
    )


def test_online_cli_defaults_to_all_architectures_and_24h_holdout() -> None:
    args = build_arg_parser().parse_args([])
    assert args.architecture == "all"
    assert args.holdout_hours == 24
    assert args.epochs == 30
    assert args.folds == 6
    assert args.validation_samples == 168
    assert args.min_train_samples == 5000
    assert args.sequence_length == 256
    assert args.d_model == 128
    assert args.heads == 8
    assert args.layers == 3


def test_realized_targets_use_only_bars_after_cutoff() -> None:
    bars = _bars()
    cutoff = bars["date"].iloc[0]
    targets = _realized_targets(
        bars,
        cutoff=cutoff,
        horizons={"1h": 1, "4h": 4},
        bar_hours=1.0,
        neutral_band=0.0,
    )
    assert targets["1h"] is not None
    assert targets["1h"]["direction"] == "up"
    assert targets["4h"]["future_close"] == 104.0


def test_realized_targets_use_horizon_specific_neutral_bands() -> None:
    bars = _bars()
    cutoff = bars["date"].iloc[0]
    targets = _realized_targets(
        bars,
        cutoff=cutoff,
        horizons={"1h": 1, "4h": 4},
        bar_hours=1.0,
        neutral_band=0.0,
        neutral_bands={"1h": 0.02, "4h": 0.001},
    )
    assert targets["1h"]["direction"] == "neutral"
    assert targets["4h"]["direction"] == "up"


def test_online_score_applies_cost_only_to_executed_signal() -> None:
    realized = {"direction": "down", "return": -0.02}
    forecast = {"direction": "down", "return": -0.01, "trade_signal": {"action": "short"}}
    score = _score_forecast(forecast, realized, trading_cost_bps=10.0)
    assert score["direction_correct"] is True
    assert score["trade_action"] == "short"
    assert score["net_return"] == pytest.approx(0.019)

    hold_score = _score_forecast({"direction": "down", "return": -0.01, "trade_signal": {"action": "hold"}}, realized, 10.0)
    assert hold_score["net_return"] == 0.0
