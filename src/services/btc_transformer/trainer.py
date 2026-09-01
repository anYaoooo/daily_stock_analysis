# -*- coding: utf-8 -*-
"""Walk-forward trainer for the research BTC Transformer model zoo."""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from .dataset import SequenceData, SequenceDataset, build_sequences, latest_sequence
from .features import FEATURE_SET_VERSION, TransformerFeatureConfig, build_transformer_feature_frame
from .models import MultiTaskTransformer, ProbabilityCalibrator

try:
    import torch
    from torch import nn
    from torch.utils.data import DataLoader
except ImportError:  # pragma: no cover
    torch = None  # type: ignore[assignment]
    nn = None  # type: ignore[assignment]
    DataLoader = None  # type: ignore[assignment]


MODEL_VERSION = "btc-transformer-multitask-wf-v6-direction-calibrated"


@dataclass(frozen=True)
class TransformerTrainingConfig:
    """Research defaults sized for repeatable out-of-fold evaluation."""

    feature: TransformerFeatureConfig = field(default_factory=TransformerFeatureConfig)
    architecture: str = "patchtst"
    patch_length: int = 16
    stride: int = 8
    d_model: int = 128
    n_heads: int = 8
    layers: int = 3
    dropout: float = 0.1
    epochs: int = 30
    batch_size: int = 128
    learning_rate: float = 1e-4
    weight_decay: float = 1e-4
    folds: int = 12
    min_train_samples: int = 5000
    validation_samples: int = 168
    purge_samples: int = 48
    seed: int = 7
    device: str = "cpu"
    class_weighted_loss: bool = True
    class_weight_power: float = 0.5
    target_clip_sigma: float = 5.0
    return_loss_weight: float = 1.0
    volatility_loss_weight: float = 0.3
    direction_loss_weight: float = 1.0
    regime_loss_weight: float = 0.0
    direction_consistency_weight: float = 0.0
    trading_cost_bps: float = 10.0
    min_signal_edge_bps: float = 5.0
    signal_confidence_threshold: float = 0.55

    def __post_init__(self) -> None:
        if str(self.architecture).lower() not in {"patchtst", "itransformer", "fusion"}:
            raise ValueError("architecture must be patchtst, itransformer, or fusion")
        for name in ("epochs", "batch_size", "folds", "min_train_samples", "validation_samples", "purge_samples"):
            object.__setattr__(self, name, max(1, int(getattr(self, name))))
        object.__setattr__(self, "purge_samples", max(self.purge_samples, max(self.feature.horizons.values())))
        object.__setattr__(self, "learning_rate", max(1e-8, float(self.learning_rate)))
        object.__setattr__(self, "weight_decay", max(0.0, float(self.weight_decay)))
        object.__setattr__(self, "class_weighted_loss", bool(self.class_weighted_loss))
        object.__setattr__(self, "class_weight_power", min(1.0, max(0.0, float(self.class_weight_power))))
        object.__setattr__(self, "target_clip_sigma", max(1.0, float(self.target_clip_sigma)))
        for name in ("return_loss_weight", "volatility_loss_weight", "direction_loss_weight", "regime_loss_weight"):
            object.__setattr__(self, name, max(0.0, float(getattr(self, name))))
        object.__setattr__(self, "direction_consistency_weight", max(0.0, float(self.direction_consistency_weight)))
        if (
            self.return_loss_weight
            + self.volatility_loss_weight
            + self.direction_loss_weight
            + self.regime_loss_weight
            + self.direction_consistency_weight
            <= 0.0
        ):
            raise ValueError("at least one training loss weight must be positive")
        object.__setattr__(self, "trading_cost_bps", max(0.0, float(self.trading_cost_bps)))
        object.__setattr__(self, "min_signal_edge_bps", max(0.0, float(self.min_signal_edge_bps)))
        object.__setattr__(self, "signal_confidence_threshold", min(1.0, max(1.0 / 3.0, float(self.signal_confidence_threshold))))


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


def _fit_target_scales(data: SequenceData, indices: Sequence[int]) -> dict[str, dict[str, float]]:
    """Fit robust target scales on the training fold only.

    The 90th percentile of absolute values is deliberately used instead of
    standard deviation: crypto returns contain genuine crash spikes that can
    otherwise make the regression heads emit implausibly large values.
    """

    selected = np.asarray(indices, dtype=np.int64)
    scales: dict[str, dict[str, float]] = {}
    for horizon in data.horizons:
        values: dict[str, float] = {}
        for name, source in (("return", data.returns[horizon]), ("volatility", data.volatilities[horizon])):
            sample = np.abs(np.asarray(source[selected], dtype=np.float64))
            finite = sample[np.isfinite(sample)]
            scale = float(np.nanpercentile(finite, 90)) if len(finite) else 0.0
            values[name] = max(scale, 1e-4)
        scales[horizon] = values
    return scales


def _scale_targets(
    data: SequenceData,
    scales: Mapping[str, Mapping[str, float]],
    *,
    clip_sigma: float,
) -> SequenceData:
    """Return a copy with robustly scaled and bounded regression targets."""

    returns = {
        horizon: np.clip(
            data.returns[horizon] / float(scales[horizon]["return"]),
            -float(clip_sigma),
            float(clip_sigma),
        ).astype(np.float32)
        for horizon in data.horizons
    }
    volatilities = {
        horizon: np.clip(
            data.volatilities[horizon] / float(scales[horizon]["volatility"]),
            0.0,
            float(clip_sigma),
        ).astype(np.float32)
        for horizon in data.horizons
    }
    return SequenceData(
        features=data.features,
        returns=returns,
        volatilities=volatilities,
        directions=data.directions,
        regimes=data.regimes,
        timestamps=data.timestamps,
        feature_names=data.feature_names,
        horizons=data.horizons,
    )


def _inverse_target(values: np.ndarray, scale: float, *, clip_sigma: float) -> np.ndarray:
    """Convert a bounded model target back to the original return unit."""

    return np.clip(np.asarray(values, dtype=np.float64), -float(clip_sigma), float(clip_sigma)) * float(scale)


def _class_weights(labels: np.ndarray, class_count: int, *, power: float = 0.5) -> Any:
    """Return softened inverse-frequency weights fitted on a training fold only."""

    counts = np.bincount(np.asarray(labels, dtype=np.int64), minlength=class_count).astype(np.float64)
    present = counts > 0
    weights = np.ones(class_count, dtype=np.float32)
    if present.any():
        inverse_frequency = counts[present].sum() / (float(present.sum()) * counts[present])
        weights[present] = np.power(inverse_frequency, min(1.0, max(0.0, float(power))))
        weights[present] = np.clip(weights[present], 0.25, 4.0)
        weights[present] /= max(float(weights[present].mean()), 1e-8)
    return torch.tensor(weights, dtype=torch.float32)


def _prior_correct_direction_logits(
    logits: np.ndarray,
    class_weights: Optional[Any] = None,
) -> np.ndarray:
    """Undo weighted-CE class-prior distortion before natural-probability scoring.

    Weighted cross entropy optimizes probabilities proportional to
    ``class_weight * p(class|x)``. Subtracting ``log(class_weight)`` restores
    the natural-prior posterior used by ordinary accuracy and Brier metrics.
    """

    values = np.asarray(logits, dtype=np.float64)
    if class_weights is None:
        return values
    if hasattr(class_weights, "detach"):
        weights = class_weights.detach().cpu().numpy()
    else:
        weights = np.asarray(class_weights, dtype=np.float64)
    weights = np.asarray(weights, dtype=np.float64).reshape(-1)
    if values.ndim != 2 or weights.shape != (values.shape[1],):
        return values
    return values - np.log(np.clip(weights, 1e-8, None))[None, :]


def _direction_summary_metrics(confusion_matrix: Sequence[Sequence[int]]) -> dict[str, Any]:
    """Return class-balanced direction metrics from an actual/predicted matrix."""

    matrix = np.asarray(confusion_matrix, dtype=np.float64)
    if matrix.shape != (3, 3) or matrix.sum() <= 0:
        return {"balanced_accuracy": None, "macro_f1": None}
    recall = np.diag(matrix) / np.clip(matrix.sum(axis=1), 1.0, None)
    precision = np.diag(matrix) / np.clip(matrix.sum(axis=0), 1.0, None)
    f1 = 2.0 * precision * recall / np.clip(precision + recall, 1e-12, None)
    return {
        "balanced_accuracy": round(float(recall.mean()), 8),
        "macro_f1": round(float(f1.mean()), 8),
    }


def _frame_fingerprint(frame: pd.DataFrame) -> str:
    """Create a deterministic fingerprint for the feature/label snapshot."""

    hashed = pd.util.hash_pandas_object(frame, index=False).to_numpy(dtype=np.uint64)
    return hashlib.sha256(hashed.tobytes()).hexdigest()


def _softmax(logits: np.ndarray) -> np.ndarray:
    values = np.asarray(logits, dtype=np.float64)
    values = values - values.max(axis=1, keepdims=True)
    probabilities = np.exp(values)
    return probabilities / np.clip(probabilities.sum(axis=1, keepdims=True), 1e-12, None)


def _correlation_metrics(actual: Sequence[float], predicted: Sequence[float]) -> tuple[Optional[float], Optional[float]]:
    """Return Pearson and Spearman IC, or ``None`` for degenerate samples."""

    actual_values = np.asarray(actual, dtype=float)
    predicted_values = np.asarray(predicted, dtype=float)
    valid = np.isfinite(actual_values) & np.isfinite(predicted_values)
    actual_values = actual_values[valid]
    predicted_values = predicted_values[valid]
    if len(actual_values) < 2 or np.ptp(actual_values) <= 1e-12 or np.ptp(predicted_values) <= 1e-12:
        return None, None
    pearson = float(np.corrcoef(actual_values, predicted_values)[0, 1])
    actual_rank = pd.Series(actual_values).rank(method="average").to_numpy(dtype=float)
    predicted_rank = pd.Series(predicted_values).rank(method="average").to_numpy(dtype=float)
    spearman = float(np.corrcoef(actual_rank, predicted_rank)[0, 1])
    return round(pearson, 8), round(spearman, 8)


def derive_trade_signal(
    direction_probabilities: Sequence[float],
    predicted_return: float,
    *,
    trading_cost_bps: float = 10.0,
    min_signal_edge_bps: float = 5.0,
    confidence_threshold: float = 0.55,
) -> dict[str, Any]:
    """Convert independent model outputs into a cost-aware long/short/hold signal."""

    probabilities = np.asarray(direction_probabilities, dtype=float)
    if probabilities.shape != (3,) or not np.isfinite(probabilities).all() or probabilities.sum() <= 0:
        return {"action": "hold", "reason": "invalid_direction_probabilities"}
    probabilities = np.clip(probabilities, 0.0, None)
    probabilities /= probabilities.sum()
    predicted = float(predicted_return)
    if not np.isfinite(predicted):
        return {"action": "hold", "reason": "invalid_predicted_return"}
    confidence = float(probabilities.max())
    class_index = int(probabilities.argmax())
    direction = ("down", "neutral", "up")[class_index]
    required_return = (max(0.0, float(trading_cost_bps)) + max(0.0, float(min_signal_edge_bps))) / 10000.0
    result: dict[str, Any] = {
        "action": "hold",
        "direction": direction,
        "confidence": confidence,
        "expected_return": predicted,
        "required_return": required_return,
        "trading_cost_bps": max(0.0, float(trading_cost_bps)),
        "min_signal_edge_bps": max(0.0, float(min_signal_edge_bps)),
        "confidence_threshold": min(1.0, max(1.0 / 3.0, float(confidence_threshold))),
    }
    if confidence < result["confidence_threshold"]:
        result["reason"] = "low_confidence"
    elif class_index == 1:
        result["reason"] = "neutral_direction"
    elif abs(predicted) < required_return:
        result["reason"] = "expected_return_below_cost_buffer"
    elif (class_index == 2 and predicted <= 0) or (class_index == 0 and predicted >= 0):
        result["reason"] = "return_direction_conflict"
    else:
        result["action"] = "long" if class_index == 2 else "short"
        result["reason"] = "cost_aware_signal"
    return result


def _trading_metrics(
    actual_returns: np.ndarray,
    predicted_returns: np.ndarray,
    direction_logits: np.ndarray,
    *,
    trading_cost_bps: float,
    min_signal_edge_bps: float,
    confidence_threshold: float,
    decision_stride: int = 1,
    probability_temperature: float = 1.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Score an abstaining, cost-aware strategy on one validation fold.

    ``decision_stride`` prevents overlapping forecast windows from being
    counted as independent trades.  ``probability_temperature`` lets callers
    use a calibration fit on data strictly preceding the scored fold.
    """

    stride = max(1, int(decision_stride))
    temperature = max(float(probability_temperature), 1e-6)
    probabilities = _softmax(np.asarray(direction_logits, dtype=float)[::stride] / temperature)
    classes = probabilities.argmax(axis=1)
    confidence = probabilities.max(axis=1)
    required_return = (max(0.0, float(trading_cost_bps)) + max(0.0, float(min_signal_edge_bps))) / 10000.0
    predicted_returns = np.asarray(predicted_returns, dtype=float)[::stride]
    actual_returns = np.asarray(actual_returns, dtype=float)[::stride]
    agreement = ((classes == 2) & (predicted_returns > 0)) | ((classes == 0) & (predicted_returns < 0))
    signal_mask = (classes != 1) & (confidence >= confidence_threshold) & (np.abs(predicted_returns) >= required_return) & agreement
    positions = np.where(classes == 2, 1.0, -1.0)
    net_returns = np.where(signal_mask, positions * actual_returns - float(trading_cost_bps) / 10000.0, 0.0)
    return net_returns.astype(float), signal_mask.astype(bool)


def _summarize_trading(net_returns: Sequence[float], signal_mask: Sequence[bool]) -> dict[str, Any]:
    returns = np.asarray(net_returns, dtype=float)
    mask = np.asarray(signal_mask, dtype=bool)
    selected = returns[mask]
    trades = int(mask.sum())
    positive = float(selected[selected > 0].sum()) if trades else 0.0
    negative = float(selected[selected < 0].sum()) if trades else 0.0
    equity = returns.cumsum()
    peak = np.maximum.accumulate(np.concatenate(([0.0], equity)))
    drawdown = peak[1:] - equity if len(equity) else np.asarray([], dtype=float)
    return {
        "trades": trades,
        "decision_samples": int(len(mask)),
        "signal_rate": round(float(trades / len(mask)), 8) if len(mask) else 0.0,
        "net_return": round(float(returns.sum()), 8) if len(returns) else 0.0,
        "avg_net_return": round(float(selected.mean()), 8) if trades else None,
        "win_rate": round(float((selected > 0).mean()), 8) if trades else None,
        "profit_factor": round(float(positive / abs(negative)), 8) if negative < 0 else (None if positive == 0 else None),
        "max_drawdown": round(float(drawdown.max()), 8) if len(drawdown) else 0.0,
    }


def _loss(
    outputs: Mapping[str, Any],
    targets: Mapping[str, Mapping[str, Any]],
    horizons: Sequence[str],
    *,
    direction_weights: Optional[Mapping[str, Any]] = None,
    regime_weights: Optional[Mapping[str, Any]] = None,
    return_loss_weight: float = 0.25,
    volatility_loss_weight: float = 0.1,
    direction_loss_weight: float = 1.0,
    regime_loss_weight: float = 0.25,
    direction_consistency_weight: float = 0.0,
) -> Any:
    losses = []
    normalizer = 0.0
    for horizon in horizons:
        direction_weight = (direction_weights or {}).get(horizon)
        regime_weight = (regime_weights or {}).get(horizon)
        if direction_weight is not None:
            direction_weight = direction_weight.to(outputs["direction"][horizon].device)
        if regime_weight is not None:
            regime_weight = regime_weight.to(outputs["regime"][horizon].device)
        task_losses = (
            (float(return_loss_weight), nn.functional.smooth_l1_loss(outputs["return"][horizon], targets[horizon]["return"])),
            (float(volatility_loss_weight), nn.functional.smooth_l1_loss(outputs["volatility"][horizon], targets[horizon]["volatility"])),
            (float(direction_loss_weight), nn.functional.cross_entropy(outputs["direction"][horizon], targets[horizon]["direction"], weight=direction_weight)),
            (float(regime_loss_weight), nn.functional.cross_entropy(outputs["regime"][horizon], targets[horizon]["regime"], weight=regime_weight)),
        )
        for weight, value in task_losses:
            if weight > 0.0:
                losses.append(weight * value)
                normalizer += weight
        direction_probabilities = nn.functional.softmax(outputs["direction"][horizon], dim=-1)
        direction_score = direction_probabilities[:, 2] - direction_probabilities[:, 0]
        consistency_weight = max(0.0, float(direction_consistency_weight))
        if consistency_weight:
            losses.append(consistency_weight * nn.functional.smooth_l1_loss(torch.tanh(outputs["return"][horizon]), direction_score))
            normalizer += consistency_weight
    return torch.stack(losses).sum() / max(normalizer, 1e-8)


def _targets_to_device(
    targets: Mapping[str, Mapping[str, Any]],
    device: Any,
    *,
    non_blocking: bool = False,
) -> dict[str, dict[str, Any]]:
    return {
        horizon: {
            name: value.to(device, non_blocking=non_blocking)
            for name, value in values.items()
        }
        for horizon, values in targets.items()
    }


class WalkForwardTransformerTrainer:
    """Train one architecture per fold and return JSON-compatible diagnostics."""

    def __init__(self, config: Optional[TransformerTrainingConfig] = None) -> None:
        self.config = config or TransformerTrainingConfig()
        self.model: Optional[MultiTaskTransformer] = None
        self.feature_columns: tuple[str, ...] = ()
        self.scaler_mean: Optional[np.ndarray] = None
        self.scaler_scale: Optional[np.ndarray] = None
        self.target_scales: Optional[dict[str, dict[str, float]]] = None

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

    def _fit_model(
        self,
        train_data: SequenceData,
        train_indices: Sequence[int],
        *,
        model: Optional[MultiTaskTransformer] = None,
        direction_weights: Optional[Mapping[str, Any]] = None,
        regime_weights: Optional[Mapping[str, Any]] = None,
    ) -> MultiTaskTransformer:
        _require_torch()
        model = model or self._new_model(len(train_data.feature_names), train_data.horizons)
        model.train()
        pin_memory = str(self.config.device).startswith(("cuda", "xpu"))
        loader = DataLoader(
            SequenceDataset(train_data, train_indices),
            batch_size=self.config.batch_size,
            shuffle=True,
            pin_memory=pin_memory,
        )
        optimizer = torch.optim.AdamW(model.parameters(), lr=self.config.learning_rate, weight_decay=self.config.weight_decay)
        for _ in range(self.config.epochs):
            for inputs, targets in loader:
                inputs = inputs.to(self.config.device, non_blocking=pin_memory)
                targets = _targets_to_device(
                    targets,
                    self.config.device,
                    non_blocking=pin_memory,
                )
                optimizer.zero_grad(set_to_none=True)
                loss = _loss(
                    model(inputs),
                    targets,
                    train_data.horizons,
                    direction_weights=direction_weights,
                    regime_weights=regime_weights,
                    return_loss_weight=self.config.return_loss_weight,
                    volatility_loss_weight=self.config.volatility_loss_weight,
                    direction_loss_weight=self.config.direction_loss_weight,
                    regime_loss_weight=self.config.regime_loss_weight,
                    direction_consistency_weight=self.config.direction_consistency_weight,
                )
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
                optimizer.step()
        return model

    @staticmethod
    def _predict(model: MultiTaskTransformer, data: SequenceData, indices: Sequence[int]) -> dict[str, dict[str, np.ndarray]]:
        _require_torch()
        device = next(model.parameters()).device
        pin_memory = device.type in {"cuda", "xpu"}
        loader = DataLoader(
            SequenceDataset(data, indices),
            batch_size=512,
            shuffle=False,
            pin_memory=pin_memory,
        )
        model.eval()
        collected: dict[str, dict[str, list[np.ndarray]]] = {horizon: {name: [] for name in ("return", "volatility", "direction", "regime")} for horizon in data.horizons}
        with torch.no_grad():
            for inputs, _ in loader:
                outputs = model(inputs.to(device, non_blocking=pin_memory))
                for horizon in data.horizons:
                    collected[horizon]["return"].append(outputs["return"][horizon].cpu().numpy())
                    collected[horizon]["volatility"].append(outputs["volatility"][horizon].cpu().numpy())
                    collected[horizon]["direction"].append(outputs["direction"][horizon].cpu().numpy())
                    collected[horizon]["regime"].append(outputs["regime"][horizon].cpu().numpy())
        return {horizon: {name: np.concatenate(values, axis=0) for name, values in tasks.items()} for horizon, tasks in collected.items()}

    def build(
        self,
        bars: pd.DataFrame,
        *,
        as_of: Any = None,
        feature_columns: Optional[Sequence[str]] = None,
        feature_frame: Optional[pd.DataFrame] = None,
    ) -> dict[str, Any]:
        """Build features, evaluate purged folds, then fit the latest model.

        ``feature_columns`` is intentionally an explicit override so research
        callers can run a one-variable ablation on the exact same labels and
        walk-forward windows.  Production callers should leave it unset.
        """

        _require_torch()
        _set_seed(self.config.seed)
        frame = (
            feature_frame.copy()
            if feature_frame is not None
            else build_transformer_feature_frame(bars, config=self.config.feature, as_of=as_of)
        )
        base: dict[str, Any] = {
            "artifact_schema_version": 2,
            "model_version": MODEL_VERSION,
            "feature_set_version": FEATURE_SET_VERSION,
            "architecture": self.config.architecture,
            "mode": "offline_research",
            "participates_in_decision": False,
            "research_only": True,
            "eligible_for_promotion": False,
            "promotion_eligible": False,
            "training_config": {
                "horizons": dict(self.config.feature.horizons),
                "bar_hours": self.config.feature.bar_hours,
                "neutral_band": self.config.feature.neutral_band,
                "neutral_bands": dict(self.config.feature.neutral_bands),
                "sequence_length": self.config.feature.sequence_length,
                "patch_length": self.config.patch_length,
                "stride": self.config.stride,
                "d_model": self.config.d_model,
                "n_heads": self.config.n_heads,
                "layers": self.config.layers,
                "epochs": self.config.epochs,
                "batch_size": self.config.batch_size,
                "learning_rate": self.config.learning_rate,
                "folds": self.config.folds,
                "min_train_samples": self.config.min_train_samples,
                "validation_samples": self.config.validation_samples,
                "purge_samples": self.config.purge_samples,
                "seed": self.config.seed,
                "device": self.config.device,
                "class_weighted_loss": self.config.class_weighted_loss,
                "class_weight_power": self.config.class_weight_power,
                "target_clip_sigma": self.config.target_clip_sigma,
                "return_loss_weight": self.config.return_loss_weight,
                "volatility_loss_weight": self.config.volatility_loss_weight,
                "direction_loss_weight": self.config.direction_loss_weight,
                "regime_loss_weight": self.config.regime_loss_weight,
                "direction_consistency_weight": self.config.direction_consistency_weight,
                "trading_cost_bps": self.config.trading_cost_bps,
                "min_signal_edge_bps": self.config.min_signal_edge_bps,
                "signal_confidence_threshold": self.config.signal_confidence_threshold,
            },
            "leakage_guard": {"random_split": False, "scaler_fit_scope": "train_only", "scheme": "purged_expanding_walk_forward"},
            "trading_policy": {
                "entry": "only when direction confidence, expected return and return/class agreement all pass",
                "cost_model": "round_trip_bps",
                "abstain_on_conflict": True,
            },
            "evaluation_policy": {
                "trading_decisions": "non_overlapping_by_horizon",
                "decision_stride_bars": {
                    horizon: int(bars)
                    for horizon, bars in self.config.feature.horizons.items()
                },
                "probability_calibration": "prior_validation_folds_only",
                "calibration_for_latest_forecast": "all_historical_oof_folds",
            },
        }
        if frame.empty:
            return {**base, "data_quality": "unavailable", "forecasts": {}, "evaluations": {}, "oof_predictions": {}}
        available_features = tuple(column for column in frame.columns if column.startswith("feature_"))
        if feature_columns is None:
            feature_columns = available_features
        else:
            requested = tuple(dict.fromkeys(str(column) for column in feature_columns))
            unknown = [column for column in requested if column not in available_features]
            if unknown:
                return {
                    **base,
                    "data_quality": "invalid_features",
                    "reason": f"unknown feature columns: {unknown}",
                    "source_bar_count": int(len(frame)),
                    "feature_count": len(requested),
                    "feature_columns": list(requested),
                    "forecasts": {},
                    "evaluations": {},
                    "oof_predictions": {},
                }
            feature_columns = requested
        feature_columns = tuple(feature_columns)
        if not feature_columns:
            return {
                **base,
                "data_quality": "invalid_features",
                "reason": "no feature columns available",
                "source_bar_count": int(len(frame)),
                "feature_count": 0,
                "feature_columns": [],
                "forecasts": {},
                "evaluations": {},
                "oof_predictions": {},
            }
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
                "oof_predictions": {},
            }
        self.feature_columns = data.feature_names
        base.update(
            {
                "artifact_schema_version": 2,
                "source_bar_count": int(len(frame)),
                "source_date_start": str(frame["date"].iloc[0]) if "date" in frame.columns else None,
                "source_date_end": str(frame["date"].iloc[-1]) if "date" in frame.columns else None,
                "input_fingerprint": _frame_fingerprint(frame),
            }
        )
        splits = walk_forward_sequence_splits(data.sample_count, min_train_samples=self.config.min_train_samples, validation_samples=self.config.validation_samples, folds=self.config.folds, purge_samples=self.config.purge_samples)
        evaluations: dict[str, Any] = {
            horizon: {"fold_count": 0, "samples": 0}
            for horizon in data.horizons
        }
        oof_predictions: dict[str, list[dict[str, Any]]] = {
            horizon: [] for horizon in data.horizons
        }
        calibration_logits: dict[str, list[np.ndarray]] = {horizon: [] for horizon in data.horizons}
        calibration_labels: dict[str, list[np.ndarray]] = {horizon: [] for horizon in data.horizons}
        fold_summaries: list[dict[str, Any]] = []
        for fold_number, split in enumerate(splits):
            train_indices = np.arange(split["train_start"], split["train_end"])
            validation_indices = np.arange(split["validation_start"], split["validation_end"])
            if not len(train_indices) or not len(validation_indices):
                continue
            mean, scale = _fit_scaler(data.features[train_indices])
            target_scales = _fit_target_scales(data, train_indices)
            target_data = _scale_targets(data, target_scales, clip_sigma=self.config.target_clip_sigma)
            scaled_data = SequenceData(
                features=_scale(data.features, mean, scale), returns=target_data.returns, volatilities=target_data.volatilities, directions=data.directions, regimes=data.regimes, timestamps=data.timestamps, feature_names=data.feature_names, horizons=data.horizons
            )
            direction_weights = {
                horizon: _class_weights(
                    data.directions[horizon][train_indices],
                    3,
                    power=self.config.class_weight_power,
                )
                for horizon in data.horizons
            } if self.config.class_weighted_loss else None
            regime_weights = {
                horizon: _class_weights(
                    data.regimes[horizon][train_indices],
                    4,
                    power=self.config.class_weight_power,
                )
                for horizon in data.horizons
            } if self.config.class_weighted_loss else None
            model = self._fit_model(
                scaled_data,
                train_indices,
                direction_weights=direction_weights,
                regime_weights=regime_weights,
            )
            predictions = self._predict(model, scaled_data, validation_indices)
            fold_summaries.append({**split, "fold": fold_number + 1, "train_samples": int(len(train_indices)), "validation_samples": int(len(validation_indices))})
            for horizon in data.horizons:
                actual_return = data.returns[horizon][validation_indices]
                actual_vol = data.volatilities[horizon][validation_indices]
                predicted_return = _inverse_target(
                    predictions[horizon]["return"],
                    target_scales[horizon]["return"],
                    clip_sigma=self.config.target_clip_sigma,
                )
                predicted_vol = np.clip(
                    predictions[horizon]["volatility"],
                    0.0,
                    self.config.target_clip_sigma,
                ) * target_scales[horizon]["volatility"]
                actual_direction = data.directions[horizon][validation_indices]
                actual_regime = data.regimes[horizon][validation_indices]
                direction_logits = _prior_correct_direction_logits(
                    predictions[horizon]["direction"],
                    (direction_weights or {}).get(horizon),
                )
                direction = direction_logits.argmax(axis=1)
                regime = predictions[horizon]["regime"].argmax(axis=1)
                probabilities = _softmax(direction_logits)
                majority_label = int(np.bincount(data.directions[horizon][train_indices], minlength=3).argmax())
                calibration_temperature = 1.0
                if calibration_logits[horizon]:
                    fold_calibrator = ProbabilityCalibrator()
                    fold_calibrator.fit(
                        np.concatenate(calibration_logits[horizon], axis=0),
                        np.concatenate(calibration_labels[horizon], axis=0),
                    )
                    calibration_temperature = fold_calibrator.temperature
                net_returns, signal_mask = _trading_metrics(
                    actual_return,
                    predicted_return,
                    direction_logits,
                    trading_cost_bps=self.config.trading_cost_bps,
                    min_signal_edge_bps=self.config.min_signal_edge_bps,
                    confidence_threshold=self.config.signal_confidence_threshold,
                    decision_stride=self.config.feature.horizons[horizon],
                    probability_temperature=calibration_temperature,
                )
                calibration_logits[horizon].append(direction_logits)
                calibration_labels[horizon].append(actual_direction)
                current = evaluations[horizon]
                current["fold_count"] += 1
                current["samples"] += int(len(validation_indices))
                current.setdefault("return_mae", []).extend(np.abs(predicted_return - actual_return).tolist())
                current.setdefault("actual_returns", []).extend(actual_return.tolist())
                current.setdefault("predicted_returns", []).extend(predicted_return.tolist())
                current.setdefault("volatility_mae", []).extend(np.abs(predicted_vol - actual_vol).tolist())
                current.setdefault("direction_correct", []).extend((direction == actual_direction).tolist())
                current.setdefault("regime_correct", []).extend((regime == actual_regime).tolist())
                current.setdefault("majority_direction_correct", []).extend((majority_label == actual_direction).tolist())
                confusion = current.setdefault("direction_confusion_matrix", [[0, 0, 0] for _ in range(3)])
                predicted_counts = current.setdefault("predicted_direction_counts", [0, 0, 0])
                actual_counts = current.setdefault("actual_direction_counts", [0, 0, 0])
                for actual_label, predicted_label in zip(actual_direction, direction):
                    actual_index = int(actual_label)
                    predicted_index = int(predicted_label)
                    confusion[actual_index][predicted_index] += 1
                    actual_counts[actual_index] += 1
                    predicted_counts[predicted_index] += 1
                current.setdefault("direction_brier", []).extend(
                    np.mean(np.square(probabilities - np.eye(3, dtype=float)[actual_direction]), axis=1).tolist()
                )
                current.setdefault("trade_net_returns", []).extend(net_returns.tolist())
                current.setdefault("trade_signal_mask", []).extend(signal_mask.tolist())
                current.setdefault("trading_calibration_temperatures", []).append(
                    round(float(calibration_temperature), 8)
                )
                # Keep one JSON-compatible row per validation sample.  This is
                # deliberately collected before aggregate metrics are reduced
                # so downstream analysis can reproduce any slice or ablation.
                direction_labels = (-1, 0, 1)
                regime_labels = ("trend_up", "trend_down", "high_volatility", "sideways")
                oof_predictions[horizon].extend(
                    {
                        "fold": int(fold_number + 1),
                        "sample_index": int(sample_index),
                        "timestamp": str(data.timestamps[sample_index]),
                        "actual_return": float(actual_return[offset]),
                        "predicted_return": float(predicted_return[offset]),
                        "actual_volatility": float(actual_vol[offset]),
                        "predicted_volatility": float(predicted_vol[offset]),
                        "actual_direction": int(direction_labels[int(actual_direction[offset])]),
                        "predicted_direction": int(direction_labels[int(direction[offset])]),
                        "actual_regime": regime_labels[int(actual_regime[offset])],
                        "predicted_regime": regime_labels[int(regime[offset])],
                        "direction_probabilities": [float(value) for value in probabilities[offset]],
                    }
                    for offset, sample_index in enumerate(validation_indices)
                )
        for horizon, summary in evaluations.items():
            actual_returns = summary.pop("actual_returns", [])
            predicted_returns = summary.pop("predicted_returns", [])
            pearson_ic, spearman_ic = _correlation_metrics(actual_returns, predicted_returns)
            for key, output_key in (
                ("return_mae", "return_mae"),
                ("volatility_mae", "volatility_mae"),
                ("direction_correct", "direction_accuracy"),
                ("regime_correct", "regime_accuracy"),
                ("majority_direction_correct", "majority_direction_accuracy"),
                ("direction_brier", "direction_brier"),
            ):
                values = summary.pop(key, [])
                summary[output_key] = round(float(np.mean(values)), 8) if values else None
            summary["pearson_ic"] = pearson_ic
            summary["spearman_ic"] = spearman_ic
            summary.update(_direction_summary_metrics(summary.get("direction_confusion_matrix", [])))
            total_predictions = max(1, sum(summary.get("actual_direction_counts", [])))
            summary["actual_direction_distribution"] = [
                round(float(value / total_predictions), 8)
                for value in summary.get("actual_direction_counts", [])
            ]
            summary["predicted_direction_distribution"] = [
                round(float(value / total_predictions), 8)
                for value in summary.get("predicted_direction_counts", [])
            ]
            trade_returns = summary.pop("trade_net_returns", [])
            trade_mask = summary.pop("trade_signal_mask", [])
            summary["trading"] = _summarize_trading(trade_returns, trade_mask)
            summary["trading"]["calibration_temperatures"] = summary.pop(
                "trading_calibration_temperatures", []
            )
        if data.sample_count < self.config.min_train_samples:
            return {**base, "data_quality": "insufficient", "source_sample_count": data.sample_count, "feature_count": len(feature_columns), "feature_columns": list(feature_columns), "forecasts": {}, "evaluations": evaluations, "oof_predictions": oof_predictions, "walk_forward": {"folds": fold_summaries}}

        # Fit final model on all complete samples. Scaling is fitted once here
        # for inference and is never reused to score a historical fold.
        self.scaler_mean, self.scaler_scale = _fit_scaler(data.features)
        target_scales = _fit_target_scales(data, np.arange(data.sample_count))
        self.target_scales = target_scales
        target_data = _scale_targets(data, target_scales, clip_sigma=self.config.target_clip_sigma)
        scaled_data = SequenceData(features=_scale(data.features, self.scaler_mean, self.scaler_scale), returns=target_data.returns, volatilities=target_data.volatilities, directions=data.directions, regimes=data.regimes, timestamps=data.timestamps, feature_names=data.feature_names, horizons=data.horizons)
        direction_weights = {
            horizon: _class_weights(
                data.directions[horizon],
                3,
                power=self.config.class_weight_power,
            )
            for horizon in data.horizons
        } if self.config.class_weighted_loss else None
        regime_weights = {
            horizon: _class_weights(
                data.regimes[horizon],
                4,
                power=self.config.class_weight_power,
            )
            for horizon in data.horizons
        } if self.config.class_weighted_loss else None
        self.model = self._fit_model(
            scaled_data,
            np.arange(data.sample_count),
            direction_weights=direction_weights,
            regime_weights=regime_weights,
        )
        latest_window, latest_at = latest_sequence(frame, sequence_length=self.config.feature.sequence_length, feature_columns=feature_columns)
        latest_scaled = _scale(latest_window[None, ...], self.scaler_mean, self.scaler_scale)
        latest_data = SequenceData(features=latest_scaled, returns={h: np.zeros(1, dtype=np.float32) for h in data.horizons}, volatilities={h: np.zeros(1, dtype=np.float32) for h in data.horizons}, directions={h: np.zeros(1, dtype=np.int64) for h in data.horizons}, regimes={h: np.zeros(1, dtype=np.int64) for h in data.horizons}, timestamps=np.asarray([latest_at]), feature_names=data.feature_names, horizons=data.horizons)
        predicted = self._predict(self.model, latest_data, [0])
        forecasts: dict[str, Any] = {}
        for horizon in data.horizons:
            logits = _prior_correct_direction_logits(
                predicted[horizon]["direction"][0:1],
                (direction_weights or {}).get(horizon),
            )
            calibrator = ProbabilityCalibrator()
            if calibration_logits[horizon]:
                calibrator.fit(
                    np.concatenate(calibration_logits[horizon], axis=0),
                    np.concatenate(calibration_labels[horizon], axis=0),
                )
            probabilities = calibrator.transform(logits)[0]
            predicted_return = float(
                _inverse_target(
                    predicted[horizon]["return"][0:1],
                    target_scales[horizon]["return"],
                    clip_sigma=self.config.target_clip_sigma,
                )[0]
            )
            predicted_volatility = float(
                np.clip(predicted[horizon]["volatility"][0], 0.0, self.config.target_clip_sigma)
                * target_scales[horizon]["volatility"]
            )
            forecasts[horizon] = {
                "return": predicted_return,
                "volatility": predicted_volatility,
                "direction_probabilities": probabilities.tolist(),
                "direction": ("down", "neutral", "up")[int(probabilities.argmax())],
                "regime": ("trend_up", "trend_down", "high_volatility", "sideways")[int(predicted[horizon]["regime"][0].argmax())],
                "calibration_temperature": calibrator.temperature,
                "trade_signal": derive_trade_signal(
                    probabilities,
                    predicted_return,
                    trading_cost_bps=self.config.trading_cost_bps,
                    min_signal_edge_bps=self.config.min_signal_edge_bps,
                    confidence_threshold=self.config.signal_confidence_threshold,
                ),
                "as_of": str(latest_at),
            }
        return {
            **base,
            "data_quality": "available",
            "source_sample_count": data.sample_count,
            "feature_count": len(feature_columns),
            "feature_columns": list(feature_columns),
            "target_scales": target_scales,
            "forecasts": forecasts,
            "evaluations": evaluations,
            "oof_predictions": oof_predictions,
            "walk_forward": {"folds": fold_summaries},
            "ensemble_note": "Use ensemble_forecasts with separately trained, calibrated model forecasts; horizons are not model names.",
        }


__all__ = [
    "MODEL_VERSION",
    "TransformerTrainingConfig",
    "WalkForwardTransformerTrainer",
    "derive_trade_signal",
    "walk_forward_sequence_splits",
]
