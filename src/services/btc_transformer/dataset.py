# -*- coding: utf-8 -*-
"""Leakage-aware fixed-length sequence datasets for BTC multi-task training."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

try:  # PyTorch remains optional for the existing non-ML application paths.
    import torch
    from torch.utils.data import Dataset
except ImportError:  # pragma: no cover - exercised only in minimal deployments
    torch = None  # type: ignore[assignment]

    class Dataset:  # type: ignore[no-redef]
        pass


REGIME_LABELS = ("trend_up", "trend_down", "high_volatility", "sideways")
REGIME_TO_INDEX = {label: index for index, label in enumerate(REGIME_LABELS)}


@dataclass(frozen=True)
class SequenceData:
    """Numpy representation used to construct train/validation datasets."""

    features: np.ndarray
    returns: dict[str, np.ndarray]
    volatilities: dict[str, np.ndarray]
    directions: dict[str, np.ndarray]
    regimes: dict[str, np.ndarray]
    timestamps: np.ndarray
    feature_names: tuple[str, ...]
    horizons: tuple[str, ...]

    @property
    def sample_count(self) -> int:
        return int(self.features.shape[0])


def _require_torch() -> Any:
    if torch is None:
        raise RuntimeError("PyTorch is required for BTC Transformer training; install torch>=2.2")
    return torch


def _feature_columns(frame: pd.DataFrame, columns: Optional[Sequence[str]]) -> list[str]:
    selected = list(columns) if columns is not None else [column for column in frame.columns if column.startswith("feature_")]
    if not selected:
        raise ValueError("no feature columns found")
    missing = [column for column in selected if column not in frame.columns]
    if missing:
        raise ValueError(f"feature columns missing from frame: {missing}")
    return selected


def build_sequences(
    frame: pd.DataFrame,
    *,
    sequence_length: int,
    horizons: Mapping[str, int] | Sequence[str],
    feature_columns: Optional[Sequence[str]] = None,
) -> SequenceData:
    """Build windows ending at ``t`` with labels strictly after ``t``.

    Rows with missing feature/label values are discarded before windowing. The
    caller must fit any scaler on the returned training subset only.
    """

    _require_torch()
    if frame is None or frame.empty:
        raise ValueError("feature frame is empty")
    length = max(2, int(sequence_length))
    columns = _feature_columns(frame, feature_columns)
    horizon_names = tuple(horizons.keys()) if isinstance(horizons, Mapping) else tuple(horizons)
    required = [*columns]
    for name in horizon_names:
        required.extend(
            [f"target_return_{name}", f"target_volatility_{name}", f"target_direction_{name}", f"target_regime_{name}"]
        )
    clean = frame.dropna(subset=required).reset_index(drop=True)
    if len(clean) < length:
        raise ValueError(f"not enough complete rows for sequence_length={length}")

    values = clean[columns].to_numpy(dtype=np.float32)
    # Build the complete window tensor in one operation.  The previous
    # row-by-row iloc loop dominated long-history training time without
    # changing the leakage boundary or label alignment.
    windows = np.lib.stride_tricks.sliding_window_view(values, length, axis=0)
    windows = np.moveaxis(windows, -1, 1).copy()
    timestamps = clean["date"].to_numpy()[length - 1 :] if "date" in clean.columns else np.arange(len(windows))
    returns: dict[str, np.ndarray] = {}
    volatilities: dict[str, np.ndarray] = {}
    directions: dict[str, np.ndarray] = {}
    regimes: dict[str, np.ndarray] = {}
    for name in horizon_names:
        returns[name] = clean[f"target_return_{name}"].to_numpy(dtype=np.float32)[length - 1 :]
        volatilities[name] = clean[f"target_volatility_{name}"].to_numpy(dtype=np.float32)[length - 1 :]
        directions[name] = clean[f"target_direction_{name}"].to_numpy(dtype=np.int64)[length - 1 :] + 1  # -1/0/1 -> 0/1/2
        regime_values = clean[f"target_regime_{name}"].astype(str).to_numpy()[length - 1 :]
        unknown = sorted(set(regime_values) - set(REGIME_TO_INDEX))
        if unknown:
            raise ValueError(f"unsupported regime label: {unknown[0]}")
        regimes[name] = np.asarray([REGIME_TO_INDEX[value] for value in regime_values], dtype=np.int64)
    return SequenceData(
        features=np.asarray(windows, dtype=np.float32),
        returns={name: np.asarray(values, dtype=np.float32) for name, values in returns.items()},
        volatilities={name: np.asarray(values, dtype=np.float32) for name, values in volatilities.items()},
        directions={name: np.asarray(values, dtype=np.int64) for name, values in directions.items()},
        regimes={name: np.asarray(values, dtype=np.int64) for name, values in regimes.items()},
        timestamps=np.asarray(timestamps),
        feature_names=tuple(columns),
        horizons=horizon_names,
    )


class SequenceDataset(Dataset):
    """Torch dataset exposing one target dictionary per sequence."""

    def __init__(self, data: SequenceData, indices: Optional[Sequence[int]] = None) -> None:
        _require_torch()
        self.data = data
        self.indices = np.arange(data.sample_count, dtype=np.int64) if indices is None else np.asarray(indices, dtype=np.int64)

    def __len__(self) -> int:
        return int(len(self.indices))

    def __getitem__(self, index: int) -> tuple[Any, dict[str, dict[str, Any]]]:
        position = int(self.indices[index])
        targets = {
            horizon: {
                "return": torch.tensor(self.data.returns[horizon][position], dtype=torch.float32),
                "volatility": torch.tensor(self.data.volatilities[horizon][position], dtype=torch.float32),
                "direction": torch.tensor(self.data.directions[horizon][position], dtype=torch.long),
                "regime": torch.tensor(self.data.regimes[horizon][position], dtype=torch.long),
            }
            for horizon in self.data.horizons
        }
        return torch.tensor(self.data.features[position], dtype=torch.float32), targets


def latest_sequence(
    frame: pd.DataFrame,
    *,
    sequence_length: int,
    feature_columns: Sequence[str],
) -> tuple[np.ndarray, Any]:
    """Return the latest feature-only window for inference."""

    clean = frame.dropna(subset=list(feature_columns)).reset_index(drop=True)
    length = max(2, int(sequence_length))
    if len(clean) < length:
        raise ValueError(f"not enough feature rows for sequence_length={length}")
    latest = clean.iloc[-1]
    return clean[list(feature_columns)].to_numpy(dtype=np.float32)[-length:], latest.get("date")


__all__ = ["REGIME_LABELS", "SequenceData", "SequenceDataset", "build_sequences", "latest_sequence"]
