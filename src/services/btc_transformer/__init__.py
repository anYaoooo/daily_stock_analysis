# -*- coding: utf-8 -*-
"""Research-only BTC sequence modelling components.

The package is intentionally separate from the production BTC decision path.
It can be used to compare PatchTST, iTransformer and their representation
fusion on identical, leakage-safe sequence datasets.
"""

from .dataset import REGIME_LABELS, SequenceData, SequenceDataset, build_sequences
from .features import (
    DEFAULT_TRANSFORMER_HORIZONS,
    TransformerFeatureConfig,
    build_transformer_feature_frame,
)
from .models import (
    MultiTaskTransformer,
    PatchTSTBackbone,
    ProbabilityCalibrator,
    ITransformerBackbone,
    ensemble_forecasts,
)
from .trainer import (
    TransformerTrainingConfig,
    WalkForwardTransformerTrainer,
    derive_trade_signal,
    walk_forward_sequence_splits,
)

__all__ = [
    "DEFAULT_TRANSFORMER_HORIZONS",
    "ITransformerBackbone",
    "MultiTaskTransformer",
    "PatchTSTBackbone",
    "ProbabilityCalibrator",
    "REGIME_LABELS",
    "SequenceData",
    "SequenceDataset",
    "TransformerFeatureConfig",
    "TransformerTrainingConfig",
    "WalkForwardTransformerTrainer",
    "build_sequences",
    "build_transformer_feature_frame",
    "derive_trade_signal",
    "ensemble_forecasts",
    "walk_forward_sequence_splits",
]
