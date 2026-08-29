# -*- coding: utf-8 -*-
"""Small PatchTST/iTransformer backbones and a multi-task prediction head."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

import numpy as np

try:
    import torch
    from torch import nn
except ImportError:  # pragma: no cover - import guard for non-ML deployments
    torch = None  # type: ignore[assignment]
    nn = None  # type: ignore[assignment]


def _require_torch() -> Any:
    if torch is None or nn is None:
        raise RuntimeError("PyTorch is required for BTC Transformer models; install torch>=2.2")
    return torch


class PatchTSTBackbone(nn.Module if nn is not None else object):
    """Patch-based temporal encoder (B, L, F) -> (B, d_model)."""

    def __init__(
        self,
        *,
        feature_count: int,
        sequence_length: int,
        patch_length: int = 16,
        stride: int = 8,
        d_model: int = 128,
        n_heads: int = 8,
        layers: int = 3,
        dropout: float = 0.1,
    ) -> None:
        _require_torch()
        super().__init__()
        if patch_length <= 0 or stride <= 0:
            raise ValueError("patch_length and stride must be positive")
        if d_model % n_heads != 0:
            raise ValueError("d_model must be divisible by n_heads")
        self.sequence_length = int(sequence_length)
        self.patch_length = int(patch_length)
        self.stride = int(stride)
        patch_count = max(1, (self.sequence_length - self.patch_length) // self.stride + 1)
        self.projection = nn.Linear(self.patch_length * int(feature_count), d_model)
        self.position = nn.Parameter(torch.zeros(1, patch_count, d_model))
        nn.init.trunc_normal_(self.position, std=0.02)
        layer = nn.TransformerEncoderLayer(d_model=d_model, nhead=n_heads, dim_feedforward=d_model * 4, dropout=dropout, batch_first=True, norm_first=True)
        self.encoder = nn.TransformerEncoder(layer, num_layers=max(1, int(layers)))
        self.pool_score = nn.Linear(d_model, 1)
        # Start as mean pooling; a random attention scorer is unstable when
        # research runs use only a few CPU epochs.
        nn.init.zeros_(self.pool_score.weight)
        nn.init.zeros_(self.pool_score.bias)
        self.norm = nn.LayerNorm(d_model)

    def forward(self, inputs: Any) -> Any:
        if inputs.ndim != 3:
            raise ValueError("inputs must have shape (batch, sequence, features)")
        if inputs.shape[1] < self.patch_length:
            raise ValueError("sequence is shorter than patch_length")
        patches = inputs.unfold(dimension=1, size=self.patch_length, step=self.stride)
        # unfold gives (B, patches, F, patch_length); put time inside each token.
        patches = patches.transpose(-1, -2).contiguous().flatten(start_dim=2)
        tokens = self.projection(patches)
        tokens = tokens + self.position[:, : tokens.shape[1]]
        encoded = self.encoder(tokens)
        weights = torch.softmax(self.pool_score(encoded).squeeze(-1), dim=1).unsqueeze(-1)
        return self.norm((encoded * weights).sum(dim=1))


class ITransformerBackbone(nn.Module if nn is not None else object):
    """Inverted encoder where each variable is a token over the time axis."""

    def __init__(
        self,
        *,
        feature_count: int,
        sequence_length: int,
        d_model: int = 128,
        n_heads: int = 8,
        layers: int = 3,
        dropout: float = 0.1,
    ) -> None:
        _require_torch()
        super().__init__()
        if d_model % n_heads != 0:
            raise ValueError("d_model must be divisible by n_heads")
        self.feature_count = int(feature_count)
        self.sequence_length = int(sequence_length)
        self.projection = nn.Linear(self.sequence_length, d_model)
        self.variable_position = nn.Parameter(torch.zeros(1, self.feature_count, d_model))
        nn.init.trunc_normal_(self.variable_position, std=0.02)
        layer = nn.TransformerEncoderLayer(d_model=d_model, nhead=n_heads, dim_feedforward=d_model * 4, dropout=dropout, batch_first=True, norm_first=True)
        self.encoder = nn.TransformerEncoder(layer, num_layers=max(1, int(layers)))
        self.pool_score = nn.Linear(d_model, 1)
        nn.init.zeros_(self.pool_score.weight)
        nn.init.zeros_(self.pool_score.bias)
        self.norm = nn.LayerNorm(d_model)

    def forward(self, inputs: Any) -> Any:
        if inputs.ndim != 3:
            raise ValueError("inputs must have shape (batch, sequence, features)")
        if inputs.shape[1] != self.sequence_length:
            raise ValueError(f"expected sequence length {self.sequence_length}, got {inputs.shape[1]}")
        tokens = self.projection(inputs.transpose(1, 2))
        tokens = tokens + self.variable_position[:, : tokens.shape[1]]
        encoded = self.encoder(tokens)
        weights = torch.softmax(self.pool_score(encoded).squeeze(-1), dim=1).unsqueeze(-1)
        return self.norm((encoded * weights).sum(dim=1))


class MultiTaskTransformer(nn.Module if nn is not None else object):
    """PatchTST, iTransformer or their representation fusion with shared heads."""

    def __init__(
        self,
        *,
        feature_count: int,
        sequence_length: int,
        horizons: Sequence[str],
        architecture: str = "patchtst",
        patch_length: int = 16,
        stride: int = 8,
        d_model: int = 128,
        n_heads: int = 8,
        layers: int = 3,
        dropout: float = 0.1,
    ) -> None:
        _require_torch()
        super().__init__()
        architecture = str(architecture).lower().strip()
        if architecture not in {"patchtst", "itransformer", "fusion"}:
            raise ValueError("architecture must be patchtst, itransformer, or fusion")
        self.architecture = architecture
        self.horizons = tuple(str(value) for value in horizons)
        self.patch = PatchTSTBackbone(feature_count=feature_count, sequence_length=sequence_length, patch_length=patch_length, stride=stride, d_model=d_model, n_heads=n_heads, layers=layers, dropout=dropout) if architecture in {"patchtst", "fusion"} else None
        self.inverted = ITransformerBackbone(feature_count=feature_count, sequence_length=sequence_length, d_model=d_model, n_heads=n_heads, layers=layers, dropout=dropout) if architecture in {"itransformer", "fusion"} else None
        hidden = d_model * (2 if architecture == "fusion" else 1)
        self.dropout = nn.Dropout(dropout)
        self.return_heads = nn.ModuleDict({horizon: nn.Linear(hidden, 1) for horizon in self.horizons})
        self.volatility_heads = nn.ModuleDict({horizon: nn.Linear(hidden, 1) for horizon in self.horizons})
        self.direction_heads = nn.ModuleDict({horizon: nn.Linear(hidden, 3) for horizon in self.horizons})
        self.regime_heads = nn.ModuleDict({horizon: nn.Linear(hidden, 4) for horizon in self.horizons})

    def forward(self, inputs: Any, *, return_embedding: bool = False) -> Mapping[str, Any]:
        embeddings = []
        if self.patch is not None:
            embeddings.append(self.patch(inputs))
        if self.inverted is not None:
            embeddings.append(self.inverted(inputs))
        embedding = self.dropout(torch.cat(embeddings, dim=-1) if len(embeddings) > 1 else embeddings[0])
        outputs = {
            "return": {horizon: head(embedding).squeeze(-1) for horizon, head in self.return_heads.items()},
            "volatility": {horizon: torch.nn.functional.softplus(head(embedding).squeeze(-1)) for horizon, head in self.volatility_heads.items()},
            "direction": {horizon: head(embedding) for horizon, head in self.direction_heads.items()},
            "regime": {horizon: head(embedding) for horizon, head in self.regime_heads.items()},
        }
        if return_embedding:
            outputs["embedding"] = embedding
        return outputs


class ProbabilityCalibrator:
    """Lightweight per-class temperature calibration for direction logits.

    Temperature is fitted on validation logits only. It is deliberately
    dependency-free and falls back to one when too few samples are available.
    """

    def __init__(self, minimum_samples: int = 32) -> None:
        self.minimum_samples = max(1, int(minimum_samples))
        self.temperature = 1.0

    def fit(self, logits: np.ndarray, labels: np.ndarray) -> "ProbabilityCalibrator":
        logits = np.asarray(logits, dtype=float)
        labels = np.asarray(labels, dtype=int)
        if logits.ndim != 2 or len(logits) < self.minimum_samples or len(np.unique(labels)) < 2:
            return self
        # Grid search is stable for small research folds and avoids an extra
        # calibration dependency; objective is multiclass negative log loss.
        best_temperature, best_loss = 1.0, float("inf")
        for temperature in np.linspace(0.5, 3.0, 26):
            scaled = logits / temperature
            scaled -= scaled.max(axis=1, keepdims=True)
            probabilities = np.exp(scaled)
            probabilities /= probabilities.sum(axis=1, keepdims=True)
            loss = -np.log(np.clip(probabilities[np.arange(len(labels)), labels], 1e-8, 1.0)).mean()
            if loss < best_loss:
                best_temperature, best_loss = float(temperature), float(loss)
        self.temperature = best_temperature
        return self

    def transform(self, logits: np.ndarray) -> np.ndarray:
        logits = np.asarray(logits, dtype=float) / self.temperature
        logits -= logits.max(axis=1, keepdims=True)
        probabilities = np.exp(logits)
        return probabilities / probabilities.sum(axis=1, keepdims=True)


def ensemble_forecasts(
    forecasts: Mapping[str, Mapping[str, Any]],
    *,
    weights: Optional[Mapping[str, float]] = None,
) -> dict[str, Any]:
    """Combine calibrated direction probabilities and numeric tasks.

    Inputs are expected to contain ``direction_probabilities`` as a length-3
    vector and optional ``return``/``volatility`` scalars. Weights are
    normalized over models that actually supplied a forecast.
    """

    valid = {name: value for name, value in forecasts.items() if value is not None and "direction_probabilities" in value}
    if not valid:
        return {"available": False, "models": []}
    raw_weights = {name: float((weights or {}).get(name, 1.0)) for name in valid}
    total = sum(max(value, 0.0) for value in raw_weights.values()) or 1.0
    normalized = {name: max(value, 0.0) / total for name, value in raw_weights.items()}
    direction = sum(normalized[name] * np.asarray(value["direction_probabilities"], dtype=float) for name, value in valid.items())
    direction = np.clip(direction, 0.0, None)
    direction /= direction.sum() or 1.0
    result: dict[str, Any] = {
        "available": True,
        "models": list(valid),
        "weights": normalized,
        "direction_probabilities": direction.tolist(),
    }
    for key in ("return", "volatility"):
        numeric_models = {name: value for name, value in valid.items() if value.get(key) is not None}
        if numeric_models:
            numeric_weight_total = sum(normalized[name] for name in numeric_models) or 1.0
            result[key] = float(
                sum(normalized[name] * float(value[key]) for name, value in numeric_models.items())
                / numeric_weight_total
            )
    return result


__all__ = ["ITransformerBackbone", "MultiTaskTransformer", "PatchTSTBackbone", "ProbabilityCalibrator", "ensemble_forecasts"]
