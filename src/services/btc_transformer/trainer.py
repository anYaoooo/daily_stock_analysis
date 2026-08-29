# -*- coding: utf-8 -*-
"""Walk-forward trainer for the research BTC Transformer model zoo."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from .dataset import SequenceData, SequenceDataset, build_sequences, latest_sequence
from .features import TransformerFeatureConfig, build_transformer_feature_frame
from .models import MultiTaskTransformer, ProbabilityCalibrator

try:
    import torch
    from torch import nn
    from torch.utils.data import DataLoader
except ImportError:  # pragma: no cover
    torch = None  # type: ignore[assignment]
    nn = None  # type: ignore[assignment]
    DataLoader = None  # type: ignore[assignment]


MODEL_VERSION = "btc-transformer-multitask-wf-v1"


@dataclass(frozen=True)
class TransformerTrainingConfig:
    """Conservative defaults for CPU-friendly research experiments."""

    feature: TransformerFeatureConfig = field(default_factory=TransformerFeatureConfig)
    architecture: str = "patchtst"
    patch_length: int = 16
    stride: int = 8
    d_model: int = 128
    n_heads: int = 8
    layers: int = 3
    dropout: float = 0.1
    epochs: int = 5
    batch_size: int = 128
    learning_rate: float = 1e-4
    weight_decay: float = 1e-4
    folds: int = 3
    min_train_samples: int = 336
    validation_samples: int = 48
    purge_samples: int = 48
    seed: int = 7
    device: str = "cpu"

    def __post_init__(self) -> None:
        if str(self.architecture).lower() not in {"patchtst", "itransformer", "fusion"}:
            raise ValueError("architecture must be patchtst, itransformer, or fusion")
        for name in ("epochs", "batch_size", "folds", "min_train_samples", "validation_samples", "purge_samples"):
            object.__setattr__(self, name, max(1, int(getattr(self, name))))
        object.__setattr__(self, "purge_samples", max(self.purge_samples, max(self.feature.horizons.values())))
        object.__setattr__(self, "learning_rate", max(1e-8, float(self.learning_rate)))
        object.__setattr__(self, "weight_decay", max(0.0, float(self.weight_decay)))


def walk_forward_sequence_splits(
    sample_count: int,
    *,
    min_train_samples: int,
    validation_samples: int,
    folds: int,
    purge_samples: int,
) -> list[dict[str, int]]:
    """Generate expanding train/validation slices with a purge gap."""

    minimum = max(1, int(min_train_samples))
    validation = max(1, int(validation_samples))
    purge = max(0, int(purge_samples))
    first = minimum + purge
    last = int(sample_count) - validation
    if last < first:
        return []
    starts = np.linspace(first, last, num=min(max(int(folds), 1), last - first + 1), dtype=int)
    result = []
    for start in dict.fromkeys(int(value) for value in starts):
        train_end = start - purge
        if train_end < minimum:
            continue
        result.append({"train_start": 0, "train_end": train_end, "validation_start": start, "validation_end": min(start + validation, sample_count), "purge_samples": purge})
    return result


def _require_torch() -> Any:
    if torch is None or DataLoader is None or nn is None:
        raise RuntimeError("PyTorch is required for BTC Transformer training; install torch>=2.2")
    return torch


def _set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    if torch is not None:
        torch.manual_seed(seed)


def _fit_scaler(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    flattened = values.reshape(-1, values.shape[-1]).astype(np.float64)
    mean = np.nanmean(flattened, axis=0)
    scale = np.nanstd(flattened, axis=0)
    scale = np.where(np.isfinite(scale) & (scale > 1e-8), scale, 1.0)
    mean = np.where(np.isfinite(mean), mean, 0.0)
    return mean.astype(np.float32), scale.astype(np.float32)


def _scale(values: np.ndarray, mean: np.ndarray, scale: np.ndarray) -> np.ndarray:
    return ((values - mean) / scale).astype(np.float32)


def _loss(outputs: Mapping[str, Any], targets: Mapping[str, Mapping[str, Any]], horizons: Sequence[str]) -> Any:
    losses = []
    for horizon in horizons:
        losses.extend(
            [
                nn.functional.smooth_l1_loss(outputs["return"][horizon], targets[horizon]["return"]),
                nn.functional.smooth_l1_loss(outputs["volatility"][horizon], targets[horizon]["volatility"]),
                nn.functional.cross_entropy(outputs["direction"][horizon], targets[horizon]["direction"]),
                nn.functional.cross_entropy(outputs["regime"][horizon], targets[horizon]["regime"]),
            ]
        )
    return torch.stack(losses).mean()


def _targets_to_device(targets: Mapping[str, Mapping[str, Any]], device: Any) -> dict[str, dict[str, Any]]:
    return {horizon: {name: value.to(device) for name, value in values.items()} for horizon, values in targets.items()}


class WalkForwardTransformerTrainer:
    """Train one architecture per fold and return JSON-compatible diagnostics."""

    def __init__(self, config: Optional[TransformerTrainingConfig] = None) -> None:
        self.config = config or TransformerTrainingConfig()
        self.model: Optional[MultiTaskTransformer] = None
        self.feature_columns: tuple[str, ...] = ()
        self.scaler_mean: Optional[np.ndarray] = None
        self.scaler_scale: Optional[np.ndarray] = None

    def _new_model(self, feature_count: int, horizons: Sequence[str]) -> MultiTaskTransformer:
        return MultiTaskTransformer(
            feature_count=feature_count,
            sequence_length=self.config.feature.sequence_length,
            horizons=horizons,
            architecture=self.config.architecture,
            patch_length=self.config.patch_length,
            stride=self.config.stride,
            d_model=self.config.d_model,
            n_heads=self.config.n_heads,
            layers=self.config.layers,
            dropout=self.config.dropout,
        ).to(self.config.device)

    def _fit_model(self, train_data: SequenceData, train_indices: Sequence[int], *, model: Optional[MultiTaskTransformer] = None) -> MultiTaskTransformer:
        _require_torch()
        model = model or self._new_model(len(train_data.feature_names), train_data.horizons)
        model.train()
        loader = DataLoader(SequenceDataset(train_data, train_indices), batch_size=self.config.batch_size, shuffle=False)
        optimizer = torch.optim.AdamW(model.parameters(), lr=self.config.learning_rate, weight_decay=self.config.weight_decay)
        for _ in range(self.config.epochs):
            for inputs, targets in loader:
                inputs = inputs.to(self.config.device)
                targets = _targets_to_device(targets, self.config.device)
                optimizer.zero_grad(set_to_none=True)
                loss = _loss(model(inputs), targets, train_data.horizons)
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
                optimizer.step()
        return model

    @staticmethod
    def _predict(model: MultiTaskTransformer, data: SequenceData, indices: Sequence[int]) -> dict[str, dict[str, np.ndarray]]:
        _require_torch()
        loader = DataLoader(SequenceDataset(data, indices), batch_size=512, shuffle=False)
        model.eval()
        collected: dict[str, dict[str, list[np.ndarray]]] = {horizon: {name: [] for name in ("return", "volatility", "direction", "regime")} for horizon in data.horizons}
        with torch.no_grad():
            for inputs, _ in loader:
                outputs = model(inputs.to(next(model.parameters()).device))
                for horizon in data.horizons:
                    collected[horizon]["return"].append(outputs["return"][horizon].cpu().numpy())
                    collected[horizon]["volatility"].append(outputs["volatility"][horizon].cpu().numpy())
                    collected[horizon]["direction"].append(outputs["direction"][horizon].cpu().numpy())
                    collected[horizon]["regime"].append(outputs["regime"][horizon].cpu().numpy())
        return {horizon: {name: np.concatenate(values, axis=0) for name, values in tasks.items()} for horizon, tasks in collected.items()}

    def build(self, bars: pd.DataFrame, *, as_of: Any = None) -> dict[str, Any]:
        """Build features, evaluate purged folds, then fit the latest model."""

        _require_torch()
        _set_seed(self.config.seed)
        frame = build_transformer_feature_frame(bars, config=self.config.feature, as_of=as_of)
        base: dict[str, Any] = {
            "model_version": MODEL_VERSION,
            "architecture": self.config.architecture,
            "mode": "offline_research",
            "participates_in_decision": False,
            "leakage_guard": {"random_split": False, "scaler_fit_scope": "train_only", "scheme": "purged_expanding_walk_forward"},
        }
        if frame.empty:
            return {**base, "data_quality": "unavailable", "forecasts": {}, "evaluations": {}}
        feature_columns = tuple(column for column in frame.columns if column.startswith("feature_"))
        try:
            data = build_sequences(
                frame,
                sequence_length=self.config.feature.sequence_length,
                horizons=self.config.feature.horizons,
                feature_columns=feature_columns,
            )
        except ValueError as exc:
            return {
                **base,
                "data_quality": "insufficient",
                "reason": str(exc),
                "source_bar_count": int(len(frame)),
                "feature_count": len(feature_columns),
                "feature_columns": list(feature_columns),
                "forecasts": {},
                "evaluations": {},
            }
        self.feature_columns = data.feature_names
        splits = walk_forward_sequence_splits(data.sample_count, min_train_samples=self.config.min_train_samples, validation_samples=self.config.validation_samples, folds=self.config.folds, purge_samples=self.config.purge_samples)
        evaluations: dict[str, Any] = {horizon: {"fold_count": 0, "samples": 0} for horizon in data.horizons}
        calibration_logits: dict[str, list[np.ndarray]] = {horizon: [] for horizon in data.horizons}
        calibration_labels: dict[str, list[np.ndarray]] = {horizon: [] for horizon in data.horizons}
        fold_summaries: list[dict[str, Any]] = []
        for fold_number, split in enumerate(splits):
            train_indices = np.arange(split["train_start"], split["train_end"])
            validation_indices = np.arange(split["validation_start"], split["validation_end"])
            if not len(train_indices) or not len(validation_indices):
                continue
            mean, scale = _fit_scaler(data.features[train_indices])
            scaled_data = SequenceData(
                features=_scale(data.features, mean, scale), returns=data.returns, volatilities=data.volatilities, directions=data.directions, regimes=data.regimes, timestamps=data.timestamps, feature_names=data.feature_names, horizons=data.horizons
            )
            model = self._fit_model(scaled_data, train_indices)
            predictions = self._predict(model, scaled_data, validation_indices)
            fold_summaries.append({**split, "fold": fold_number + 1, "train_samples": int(len(train_indices)), "validation_samples": int(len(validation_indices))})
            for horizon in data.horizons:
                actual_return = data.returns[horizon][validation_indices]
                actual_vol = data.volatilities[horizon][validation_indices]
                actual_direction = data.directions[horizon][validation_indices]
                actual_regime = data.regimes[horizon][validation_indices]
                direction = predictions[horizon]["direction"].argmax(axis=1)
                regime = predictions[horizon]["regime"].argmax(axis=1)
                calibration_logits[horizon].append(predictions[horizon]["direction"])
                calibration_labels[horizon].append(actual_direction)
                current = evaluations[horizon]
                current["fold_count"] += 1
                current["samples"] += int(len(validation_indices))
                current.setdefault("return_mae", []).extend(np.abs(predictions[horizon]["return"] - actual_return).tolist())
                current.setdefault("volatility_mae", []).extend(np.abs(predictions[horizon]["volatility"] - actual_vol).tolist())
                current.setdefault("direction_correct", []).extend((direction == actual_direction).tolist())
                current.setdefault("regime_correct", []).extend((regime == actual_regime).tolist())
        for horizon, summary in evaluations.items():
            for key, output_key in (("return_mae", "return_mae"), ("volatility_mae", "volatility_mae"), ("direction_correct", "direction_accuracy"), ("regime_correct", "regime_accuracy")):
                values = summary.pop(key, [])
                summary[output_key] = round(float(np.mean(values)), 8) if values else None
        if data.sample_count < self.config.min_train_samples:
            return {**base, "data_quality": "insufficient", "source_sample_count": data.sample_count, "feature_count": len(feature_columns), "feature_columns": list(feature_columns), "forecasts": {}, "evaluations": evaluations, "walk_forward": {"folds": fold_summaries}}

        # Fit final model on all complete samples. Scaling is fitted once here
        # for inference and is never reused to score a historical fold.
        self.scaler_mean, self.scaler_scale = _fit_scaler(data.features)
        scaled_data = SequenceData(features=_scale(data.features, self.scaler_mean, self.scaler_scale), returns=data.returns, volatilities=data.volatilities, directions=data.directions, regimes=data.regimes, timestamps=data.timestamps, feature_names=data.feature_names, horizons=data.horizons)
        self.model = self._fit_model(scaled_data, np.arange(data.sample_count))
        latest_window, latest_at = latest_sequence(frame, sequence_length=self.config.feature.sequence_length, feature_columns=feature_columns)
        latest_scaled = _scale(latest_window[None, ...], self.scaler_mean, self.scaler_scale)
        latest_data = SequenceData(features=latest_scaled, returns={h: np.zeros(1, dtype=np.float32) for h in data.horizons}, volatilities={h: np.zeros(1, dtype=np.float32) for h in data.horizons}, directions={h: np.zeros(1, dtype=np.int64) for h in data.horizons}, regimes={h: np.zeros(1, dtype=np.int64) for h in data.horizons}, timestamps=np.asarray([latest_at]), feature_names=data.feature_names, horizons=data.horizons)
        predicted = self._predict(self.model, latest_data, [0])
        forecasts: dict[str, Any] = {}
        for horizon in data.horizons:
            logits = predicted[horizon]["direction"][0:1]
            calibrator = ProbabilityCalibrator()
            if calibration_logits[horizon]:
                calibrator.fit(
                    np.concatenate(calibration_logits[horizon], axis=0),
                    np.concatenate(calibration_labels[horizon], axis=0),
                )
            probabilities = calibrator.transform(logits)[0]
            forecasts[horizon] = {
                "return": float(predicted[horizon]["return"][0]),
                "volatility": float(predicted[horizon]["volatility"][0]),
                "direction_probabilities": probabilities.tolist(),
                "direction": ("down", "neutral", "up")[int(probabilities.argmax())],
                "regime": ("trend_up", "trend_down", "high_volatility", "sideways")[int(predicted[horizon]["regime"][0].argmax())],
                "calibration_temperature": calibrator.temperature,
                "as_of": str(latest_at),
            }
        return {
            **base,
            "data_quality": "available",
            "source_sample_count": data.sample_count,
            "feature_count": len(feature_columns),
            "feature_columns": list(feature_columns),
            "forecasts": forecasts,
            "evaluations": evaluations,
            "walk_forward": {"folds": fold_summaries},
            "ensemble_note": "Use ensemble_forecasts with separately trained, calibrated model forecasts; horizons are not model names.",
        }


__all__ = ["MODEL_VERSION", "TransformerTrainingConfig", "WalkForwardTransformerTrainer", "walk_forward_sequence_splits"]
