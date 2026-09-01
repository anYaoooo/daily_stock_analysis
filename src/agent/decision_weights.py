# -*- coding: utf-8 -*-
"""Decision weights configuration and validation for transparent decision-making.

This module provides configurable weights for different analysis dimensions,
enabling transparent and auditable decision processes.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class DecisionWeights:
    """Configurable decision weights for multi-dimensional analysis.

    All weights should sum to 1.0 for proper normalization.
    Individual weights represent the relative importance of each analysis dimension.
    """

    technical: float = 0.40  # Technical analysis weight (indicators, patterns, levels)
    fundamental: float = 0.25  # Fundamental analysis weight (on-chain, metrics, value)
    sentiment: float = 0.20  # Market sentiment weight (news, social, fear/greed)
    risk: float = 0.15  # Risk management weight (volatility, drawdown, exposure)

    # Metadata for audit trail
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate and normalize weights after initialization."""
        self._validate_weights()
        self._normalize_weights()

    def _validate_weights(self) -> None:
        """Ensure all weights are valid (finite, non-negative)."""
        weights = {
            "technical": self.technical,
            "fundamental": self.fundamental,
            "sentiment": self.sentiment,
            "risk": self.risk,
        }

        for name, weight in weights.items():
            if not math.isfinite(weight):
                raise ValueError(f"Weight '{name}' must be finite, got {weight}")
            if weight < 0:
                raise ValueError(f"Weight '{name}' must be non-negative, got {weight}")

    def _normalize_weights(self) -> None:
        """Normalize weights to sum to 1.0."""
        total = self.technical + self.fundamental + self.sentiment + self.risk

        if total <= 0:
            raise ValueError(f"Total weight must be positive, got {total}")

        # Normalize if not already at 1.0 (with small tolerance)
        if abs(total - 1.0) > 0.001:
            logger.debug(f"Normalizing weights from sum={total:.4f} to 1.0")
            self.technical /= total
            self.fundamental /= total
            self.sentiment /= total
            self.risk /= total

    @classmethod
    def from_config(cls, config: Any) -> DecisionWeights:
        """Create weights from configuration object.

        Args:
            config: Configuration object with weight attributes

        Returns:
            DecisionWeights instance with values from config
        """
        technical = float(getattr(config, "btc_decision_weight_technical", 0.40) or 0.40)
        fundamental = float(getattr(config, "btc_decision_weight_fundamental", 0.25) or 0.25)
        sentiment = float(getattr(config, "btc_decision_weight_sentiment", 0.20) or 0.20)
        risk = float(getattr(config, "btc_decision_weight_risk", 0.15) or 0.15)

        metadata = {
            "source": "config",
            "config_class": type(config).__name__,
        }

        return cls(
            technical=technical,
            fundamental=fundamental,
            sentiment=sentiment,
            risk=risk,
            metadata=metadata,
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> DecisionWeights:
        """Create weights from dictionary.

        Args:
            data: Dictionary with weight values

        Returns:
            DecisionWeights instance
        """
        technical = float(data.get("technical", 0.40))
        fundamental = float(data.get("fundamental", 0.25))
        sentiment = float(data.get("sentiment", 0.20))
        risk = float(data.get("risk", 0.15))
        metadata = data.get("metadata", {})

        return cls(
            technical=technical,
            fundamental=fundamental,
            sentiment=sentiment,
            risk=risk,
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert weights to dictionary for serialization.

        Returns:
            Dictionary with all weight values and metadata
        """
        return {
            "technical": round(self.technical, 4),
            "fundamental": round(self.fundamental, 4),
            "sentiment": round(self.sentiment, 4),
            "risk": round(self.risk, 4),
            "total": round(self.technical + self.fundamental + self.sentiment + self.risk, 4),
            "metadata": self.metadata,
        }

    def compute_weighted_score(
        self,
        technical_score: float,
        fundamental_score: float,
        sentiment_score: float,
        risk_score: float,
    ) -> Dict[str, float]:
        """Compute weighted final score from dimension scores.

        Args:
            technical_score: Technical analysis score (0-100)
            fundamental_score: Fundamental analysis score (0-100)
            sentiment_score: Sentiment analysis score (0-100)
            risk_score: Risk assessment score (0-100)

        Returns:
            Dictionary with weighted contributions and final score
        """
        # Compute weighted contributions
        technical_contribution = self.technical * technical_score
        fundamental_contribution = self.fundamental * fundamental_score
        sentiment_contribution = self.sentiment * sentiment_score
        risk_contribution = self.risk * risk_score

        # Compute final weighted score
        final_score = (
            technical_contribution
            + fundamental_contribution
            + sentiment_contribution
            + risk_contribution
        )

        return {
            "technical_contribution": round(technical_contribution, 2),
            "fundamental_contribution": round(fundamental_contribution, 2),
            "sentiment_contribution": round(sentiment_contribution, 2),
            "risk_contribution": round(risk_contribution, 2),
            "final_score": round(final_score, 2),
            "weights_used": self.to_dict(),
        }

    def __repr__(self) -> str:
        """String representation showing all weights."""
        return (
            f"DecisionWeights(technical={self.technical:.3f}, "
            f"fundamental={self.fundamental:.3f}, "
            f"sentiment={self.sentiment:.3f}, "
            f"risk={self.risk:.3f})"
        )


def get_default_weights() -> DecisionWeights:
    """Get default decision weights.

    Returns:
        DecisionWeights with default values
    """
    return DecisionWeights(
        technical=0.40,
        fundamental=0.25,
        sentiment=0.20,
        risk=0.15,
        metadata={"source": "default"},
    )


def load_weights_from_config(config: Any) -> DecisionWeights:
    """Load decision weights from configuration, with fallback to defaults.

    Args:
        config: Configuration object

    Returns:
        DecisionWeights loaded from config or defaults
    """
    try:
        weights = DecisionWeights.from_config(config)
        logger.info(f"Loaded decision weights from config: {weights}")
        return weights
    except Exception as exc:
        logger.warning(f"Failed to load weights from config: {exc}, using defaults")
        return get_default_weights()
