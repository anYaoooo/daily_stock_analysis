# -*- coding: utf-8 -*-
"""Tests for decision weights module."""

import pytest

from src.agent.decision_weights import (
    DecisionWeights,
    get_default_weights,
    load_weights_from_config,
)


class TestDecisionWeights:
    """Test DecisionWeights class."""

    def test_default_initialization(self):
        """Test default weight initialization."""
        weights = DecisionWeights()

        assert weights.technical == 0.40
        assert weights.fundamental == 0.25
        assert weights.sentiment == 0.20
        assert weights.risk == 0.15

        # Should sum to 1.0
        total = weights.technical + weights.fundamental + weights.sentiment + weights.risk
        assert abs(total - 1.0) < 0.001

    def test_custom_initialization(self):
        """Test custom weight initialization."""
        weights = DecisionWeights(
            technical=0.50,
            fundamental=0.30,
            sentiment=0.15,
            risk=0.05,
        )

        assert weights.technical == 0.50
        assert weights.fundamental == 0.30
        assert weights.sentiment == 0.15
        assert weights.risk == 0.05

    def test_normalization(self):
        """Test weight normalization."""
        weights = DecisionWeights(
            technical=2.0,
            fundamental=1.0,
            sentiment=0.5,
            risk=0.5,
        )

        # Should be normalized to sum to 1.0
        total = weights.technical + weights.fundamental + weights.sentiment + weights.risk
        assert abs(total - 1.0) < 0.001

        # Check proportions are maintained
        assert abs(weights.technical - 0.50) < 0.001  # 2.0/4.0
        assert abs(weights.fundamental - 0.25) < 0.001  # 1.0/4.0
        assert abs(weights.sentiment - 0.125) < 0.001  # 0.5/4.0
        assert abs(weights.risk - 0.125) < 0.001  # 0.5/4.0

    def test_negative_weight_raises_error(self):
        """Test that negative weights raise ValueError."""
        with pytest.raises(ValueError, match="must be non-negative"):
            DecisionWeights(technical=-0.1, fundamental=0.5, sentiment=0.3, risk=0.3)

    def test_zero_total_raises_error(self):
        """Test that zero total raises ValueError."""
        with pytest.raises(ValueError, match="must be positive"):
            DecisionWeights(technical=0, fundamental=0, sentiment=0, risk=0)

    def test_infinite_weight_raises_error(self):
        """Test that infinite weights raise ValueError."""
        with pytest.raises(ValueError, match="must be finite"):
            DecisionWeights(technical=float('inf'), fundamental=0.25, sentiment=0.20, risk=0.15)

    def test_to_dict(self):
        """Test conversion to dictionary."""
        weights = DecisionWeights(
            technical=0.40,
            fundamental=0.25,
            sentiment=0.20,
            risk=0.15,
        )

        result = weights.to_dict()

        assert result["technical"] == 0.40
        assert result["fundamental"] == 0.25
        assert result["sentiment"] == 0.20
        assert result["risk"] == 0.15
        assert abs(result["total"] - 1.0) < 0.001
        assert "metadata" in result

    def test_from_dict(self):
        """Test creation from dictionary."""
        data = {
            "technical": 0.50,
            "fundamental": 0.30,
            "sentiment": 0.15,
            "risk": 0.05,
            "metadata": {"source": "test"},
        }

        weights = DecisionWeights.from_dict(data)

        assert weights.technical == 0.50
        assert weights.fundamental == 0.30
        assert weights.sentiment == 0.15
        assert weights.risk == 0.05
        assert weights.metadata["source"] == "test"

    def test_compute_weighted_score(self):
        """Test weighted score computation."""
        weights = DecisionWeights(
            technical=0.40,
            fundamental=0.25,
            sentiment=0.20,
            risk=0.15,
        )

        result = weights.compute_weighted_score(
            technical_score=80.0,
            fundamental_score=70.0,
            sentiment_score=60.0,
            risk_score=90.0,
        )

        # Check contributions
        assert result["technical_contribution"] == 32.0  # 0.40 * 80
        assert result["fundamental_contribution"] == 17.5  # 0.25 * 70
        assert result["sentiment_contribution"] == 12.0  # 0.20 * 60
        assert result["risk_contribution"] == 13.5  # 0.15 * 90

        # Check final score
        expected_final = 32.0 + 17.5 + 12.0 + 13.5
        assert result["final_score"] == expected_final

        # Check weights are included
        assert "weights_used" in result

    def test_from_config_with_mock(self):
        """Test loading from config object."""
        class MockConfig:
            btc_decision_weight_technical = 0.50
            btc_decision_weight_fundamental = 0.30
            btc_decision_weight_sentiment = 0.15
            btc_decision_weight_risk = 0.05

        config = MockConfig()
        weights = DecisionWeights.from_config(config)

        assert weights.technical == 0.50
        assert weights.fundamental == 0.30
        assert weights.sentiment == 0.15
        assert weights.risk == 0.05
        assert weights.metadata["source"] == "config"

    def test_from_config_with_defaults(self):
        """Test loading from config with missing attributes."""
        class MockConfig:
            pass

        config = MockConfig()
        weights = DecisionWeights.from_config(config)

        # Should use defaults
        assert weights.technical == 0.40
        assert weights.fundamental == 0.25
        assert weights.sentiment == 0.20
        assert weights.risk == 0.15

    def test_get_default_weights(self):
        """Test getting default weights."""
        weights = get_default_weights()

        assert weights.technical == 0.40
        assert weights.fundamental == 0.25
        assert weights.sentiment == 0.20
        assert weights.risk == 0.15
        assert weights.metadata["source"] == "default"

    def test_repr(self):
        """Test string representation."""
        weights = DecisionWeights()
        repr_str = repr(weights)

        assert "DecisionWeights" in repr_str
        assert "technical=0.400" in repr_str
        assert "fundamental=0.250" in repr_str
        assert "sentiment=0.200" in repr_str
        assert "risk=0.150" in repr_str


class TestLoadWeightsFromConfig:
    """Test load_weights_from_config function."""

    def test_load_with_valid_config(self):
        """Test loading with valid config."""
        class MockConfig:
            btc_decision_weight_technical = 0.45
            btc_decision_weight_fundamental = 0.30
            btc_decision_weight_sentiment = 0.15
            btc_decision_weight_risk = 0.10

        config = MockConfig()
        weights = load_weights_from_config(config)

        assert weights.technical == 0.45
        assert weights.fundamental == 0.30
        assert weights.sentiment == 0.15
        assert weights.risk == 0.10

    def test_load_with_invalid_config_falls_back_to_defaults(self):
        """Test loading with invalid config falls back to defaults."""
        class BadConfig:
            btc_decision_weight_technical = "invalid"

        config = BadConfig()
        weights = load_weights_from_config(config)

        # Should fall back to defaults
        assert weights.technical == 0.40
        assert weights.fundamental == 0.25
        assert weights.sentiment == 0.20
        assert weights.risk == 0.15
        assert weights.metadata["source"] == "default"
