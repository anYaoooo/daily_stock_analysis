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
    build_sequences,
    build_transformer_feature_frame,
    walk_forward_sequence_splits,
)

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


def test_transformer_cli_exposes_training_device() -> None:
    args = build_arg_parser().parse_args(["--device", "cuda:0"])
    assert args.device == "cuda:0"
