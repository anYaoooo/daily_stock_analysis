# -*- coding: utf-8 -*-
"""Regression tests for crypto derivative indicator semantics."""

from src.indicators.crypto_indicators import FundingRateAnalysis, OpenInterestAnalysis


def test_funding_rate_uses_fraction_units_and_realistic_settlement_windows() -> None:
    result = FundingRateAnalysis.analyze([0.0006, 0.0002, 0.0002, 0.0001, 0.0001])

    assert result.avg_rate_24h == 0.0003333333333333333
    assert result.avg_rate_7d == 0.00024
    assert result.extremity == "extreme_long"
    assert result.trend == "positive"


def test_open_interest_price_direction_is_symmetric() -> None:
    short_covering = OpenInterestAnalysis.analyze(90, 100, 110, 3.0)
    long_liquidation = OpenInterestAnalysis.analyze(90, 100, 110, -3.0)

    assert short_covering.price_oi_divergence == "bullish"
    assert "空头回补" in short_covering.interpretation
    assert long_liquidation.price_oi_divergence == "bearish"
    assert "多头平仓" in long_liquidation.interpretation
