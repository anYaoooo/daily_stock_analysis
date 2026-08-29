# -*- coding: utf-8 -*-
"""Deterministic contracts for the offline BTC Transformer research module."""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

torch = pytest.importorskip("torch")

from src.services.btc_transformer import (  # noqa: E402
    ITransformerBackbone,
    MultiTaskTransformer,
    PatchTSTBackbone,
    TransformerFeatureConfig,
    TransformerTrainingConfig,
    WalkForwardTransformerTrainer,
    build_sequences,
    build_transformer_feature_frame,
    derive_trade_signal,
    walk_forward_sequence_splits,
)
from src.services.btc_transformer.dataset import SequenceData  # noqa: E402
from src.services.btc_transformer.trainer import _fit_target_scales, _inverse_target, _scale_targets  # noqa: E402

from scripts.train_btc_transformer import build_arg_parser  # noqa: E402


def _bars(count: int = 180) -> pd.DataFrame:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    close = 90_000.0
    rows = []
    for index in range(count):
        open_price = close
        close *= 1.0 + 0.001 * np.sin(index / 7.0) + 0.0002
        rows.append(
            {
                "date": start + timedelta(minutes=index * 5),
                "open": open_price,
                "high": max(open_price, close) * 1.001,
                "low": min(open_price, close) * 0.999,
                "close": close,
                "volume": 1000.0 + index % 11,
            }
        )
    frame = pd.DataFrame(rows)
    frame.attrs["fetched_at"] = (start + timedelta(minutes=count * 5 + 5)).isoformat()
    return frame


def test_feature_frame_filters_open_bar_and_keeps_future_targets_separate() -> None:
    bars = _bars()
    bars.attrs["fetched_at"] = bars.iloc[-1]["date"] + pd.Timedelta(minutes=2)
    frame = build_transformer_feature_frame(
        bars,
        config=TransformerFeatureConfig(horizons={"15m": 3}, sequence_length=32, bar_hours=5 / 60),
    )
    assert len(frame) == len(bars) - 1
    assert frame.iloc[-1]["date"] == bars.iloc[-2]["date"]
    assert all(column.startswith("feature_") or column.startswith("target_") or column in {"date", "reference_close"} for column in frame)


def test_sequence_dataset_shapes_and_label_encoding() -> None:
    frame = build_transformer_feature_frame(
        _bars(360),
        config=TransformerFeatureConfig(horizons={"15m": 3, "1h": 12}, sequence_length=32, bar_hours=5 / 60),
    )
    data = build_sequences(frame, sequence_length=32, horizons={"15m": 3, "1h": 12})
    assert data.features.ndim == 3
    assert data.features.shape[1] == 32
    assert data.features.shape[2] == len(data.feature_names)
    assert set(np.unique(data.directions["15m"])).issubset({0, 1, 2})
    assert set(np.unique(data.regimes["1h"])).issubset({0, 1, 2, 3})


def test_backbones_and_fusion_emit_all_multi_task_heads() -> None:
    inputs = torch.randn(2, 32, 6)
    assert PatchTSTBackbone(feature_count=6, sequence_length=32, patch_length=8, stride=4, d_model=16, n_heads=4, layers=1)(inputs).shape == (2, 16)
    assert ITransformerBackbone(feature_count=6, sequence_length=32, d_model=16, n_heads=4, layers=1)(inputs).shape == (2, 16)
    outputs = MultiTaskTransformer(feature_count=6, sequence_length=32, horizons=("15m", "1h"), architecture="fusion", d_model=16, n_heads=4, layers=1)(inputs)
    assert set(outputs) == {"return", "volatility", "direction", "regime"}
    assert outputs["direction"]["1h"].shape == (2, 3)
    assert outputs["regime"]["15m"].shape == (2, 4)


def test_walk_forward_sequence_splits_preserve_purge_gap() -> None:
    splits = walk_forward_sequence_splits(500, min_train_samples=200, validation_samples=40, folds=3, purge_samples=48)
    assert len(splits) == 3
    assert all(item["train_end"] + item["purge_samples"] <= item["validation_start"] for item in splits)
    assert all(item["train_start"] == 0 for item in splits)


def test_training_config_purge_covers_longest_horizon() -> None:
    config = TransformerTrainingConfig(
        feature=TransformerFeatureConfig(horizons={"1h": 1, "24h": 100}, sequence_length=32),
        purge_samples=2,
    )
    assert config.purge_samples == 100


def test_trade_signal_abstains_until_cost_confidence_and_direction_agree() -> None:
    low_confidence = derive_trade_signal([0.34, 0.33, 0.33], 0.02, trading_cost_bps=10, min_signal_edge_bps=5, confidence_threshold=0.55)
    assert low_confidence["action"] == "hold"
    assert low_confidence["reason"] == "low_confidence"

    conflicting = derive_trade_signal([0.10, 0.10, 0.80], -0.02, trading_cost_bps=10, min_signal_edge_bps=5, confidence_threshold=0.55)
    assert conflicting["action"] == "hold"
    assert conflicting["reason"] == "return_direction_conflict"

    signal = derive_trade_signal([0.10, 0.10, 0.80], 0.02, trading_cost_bps=10, min_signal_edge_bps=5, confidence_threshold=0.55)
    assert signal["action"] == "long"
    assert signal["reason"] == "cost_aware_signal"

    invalid_return = derive_trade_signal([0.10, 0.10, 0.80], float("nan"))
    assert invalid_return == {"action": "hold", "reason": "invalid_predicted_return"}


def test_training_config_exposes_cost_aware_signal_defaults() -> None:
    config = TransformerTrainingConfig()
    assert config.class_weighted_loss is True
    assert config.trading_cost_bps == 10.0
    assert config.min_signal_edge_bps == 5.0
    assert config.signal_confidence_threshold == 0.55
    assert config.direction_consistency_weight == 0.0


def test_transformer_cli_exposes_training_device() -> None:
    args = build_arg_parser().parse_args(["--device", "cuda:0"])
    assert args.device == "cuda:0"


def test_transformer_cli_exposes_neutral_band() -> None:
    args = build_arg_parser().parse_args(["--neutral-band-bps", "35"])
    assert args.neutral_band_bps == 35


def test_transformer_cli_exposes_target_clip() -> None:
    args = build_arg_parser().parse_args(["--target-clip-sigma", "4"])
    assert args.target_clip_sigma == 4


def test_transformer_cli_disables_direction_consistency_by_default() -> None:
    assert build_arg_parser().parse_args([]).direction_consistency_weight == 0.0


def test_training_config_exposes_direction_consistency_weight() -> None:
    config = TransformerTrainingConfig(direction_consistency_weight=0.4)
    assert config.direction_consistency_weight == 0.4


def test_target_scaling_is_train_only_and_bounded() -> None:
    data = SequenceData(
        features=np.zeros((6, 2, 1), dtype=np.float32),
        returns={"1h": np.array([0.01, -0.02, 0.03, 0.04, 10.0, 0.0], dtype=np.float32)},
        volatilities={"1h": np.array([0.01, 0.02, 0.03, 0.04, 10.0, 0.0], dtype=np.float32)},
        directions={"1h": np.zeros(6, dtype=np.int64)},
        regimes={"1h": np.zeros(6, dtype=np.int64)},
        timestamps=np.arange(6),
        feature_names=("feature_x",),
        horizons=("1h",),
    )
    scales = _fit_target_scales(data, [0, 1, 2, 3])
    assert scales["1h"]["return"] < 0.1
    assert scales["1h"]["volatility"] < 0.1
    scaled = _scale_targets(data, scales, clip_sigma=5.0)
    assert float(np.max(np.abs(scaled.returns["1h"]))) <= 5.0
    assert float(np.max(scaled.volatilities["1h"])) <= 5.0
    restored = _inverse_target(np.array([2.0]), scales["1h"]["return"], clip_sigma=5.0)
    assert restored[0] == pytest.approx(2.0 * scales["1h"]["return"])


def test_training_result_records_effective_configuration() -> None:
    config = TransformerTrainingConfig(
        feature=TransformerFeatureConfig(horizons={"1h": 1}, sequence_length=32),
        architecture="itransformer",
        d_model=16,
        n_heads=4,
        layers=1,
        epochs=2,
        folds=1,
        min_train_samples=20,
        validation_samples=8,
        purge_samples=1,
        device="cpu",
    )
    # The configuration is exposed through the trainer payload even before a
    # dataset is available, which keeps constrained runs auditable.
    trainer = WalkForwardTransformerTrainer(config)
    result = trainer.build(pd.DataFrame())
    assert result["training_config"]["sequence_length"] == 32
    assert result["training_config"]["device"] == "cpu"
