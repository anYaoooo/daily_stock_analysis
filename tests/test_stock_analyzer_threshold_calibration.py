# -*- coding: utf-8 -*-
"""Regression tests for state-aware stock signal thresholds."""

from datetime import date, timedelta
from unittest.mock import patch

import pandas as pd

from src.stock_analyzer import (
    BuySignal,
    MACDStatus,
    RSIStatus,
    StockTrendAnalyzer,
    TrendAnalysisResult,
    TrendStatus,
    VolumeStatus,
)


def _bars(step: float = 0.5, spread: float = 0.01) -> pd.DataFrame:
    prices = [100.0 + step * index for index in range(60)]
    return pd.DataFrame(
        {
            "date": [date(2026, 1, 1) + timedelta(days=index) for index in range(60)],
            "open": prices,
            "high": [price * (1.0 + spread) for price in prices],
            "low": [price * (1.0 - spread) for price in prices],
            "close": prices,
            "volume": [100.0] * len(prices),
        }
    )


def test_threshold_profile_calibrates_phase_volatility_and_direction_independently() -> None:
    analyzer = StockTrendAnalyzer()
    with patch("src.stock_analyzer.get_config") as get_config:
        get_config.return_value.bias_threshold = 5.0
        baseline = analyzer.calibrate_thresholds(
            market_phase="intraday",
            volatility_state="normal",
            direction="long",
            market_regime="bull_trend",
        )
        lunch = analyzer.calibrate_thresholds(
            market_phase="lunch_break",
            volatility_state="normal",
            direction="long",
            market_regime="bull_trend",
        )
        extreme = analyzer.calibrate_thresholds(
            market_phase="intraday",
            volatility_state="extreme",
            direction="long",
            market_regime="bull_trend",
        )
        short = analyzer.calibrate_thresholds(
            market_phase="intraday",
            volatility_state="normal",
            direction="short",
            market_regime="bull_trend",
        )

    assert lunch["phase_multiplier"] > baseline["phase_multiplier"]
    assert lunch["bias_threshold"] > baseline["bias_threshold"]
    assert extreme["volatility_multiplier"] > baseline["volatility_multiplier"]
    assert extreme["long_entry_score"] > baseline["long_entry_score"]
    assert extreme["bias_threshold"] < baseline["bias_threshold"]
    assert short["short_entry_score"] < baseline["short_entry_score"]
    assert short["direction"] == "short"
    assert baseline["long_entry_score"] < baseline["short_entry_score"]


def test_analyze_persists_phase_regime_and_volatility_profile() -> None:
    with patch("src.stock_analyzer.get_config") as get_config:
        get_config.return_value.bias_threshold = 5.0
        result = StockTrendAnalyzer().analyze(
            _bars(step=0.2, spread=0.03),
            "600519",
            market_phase_context={"phase": "intraday", "market": "cn"},
        )

    assert result.market_phase == "intraday"
    assert result.market_regime in {"bull_trend", "range", "transition"}
    assert result.volatility_state in {"low", "normal", "high", "extreme"}
    assert result.threshold_profile["market_phase"] == result.market_phase
    assert result.threshold_profile["volatility_state"] == result.volatility_state
    assert result.signal_direction == result.threshold_profile["direction"]
    assert result.to_dict()["threshold_profile"] == result.threshold_profile


def test_invalid_phase_and_volatility_fail_back_to_neutral_profile() -> None:
    analyzer = StockTrendAnalyzer()
    with patch("src.stock_analyzer.get_config") as get_config:
        get_config.return_value.bias_threshold = 5.0
        profile = analyzer.calibrate_thresholds(
            market_phase="not-a-phase",
            volatility_state="not-a-state",
            direction="not-a-direction",
            market_regime="not-a-regime",
        )

    assert profile["market_phase"] == "unknown"
    assert profile["volatility_state"] == "normal"
    assert profile["direction"] == "neutral"
    assert profile["market_regime"] == "unknown"


def test_short_side_bias_does_not_reward_extended_downside_as_a_long_pullback() -> None:
    result = TrendAnalysisResult(
        code="600519",
        trend_status=TrendStatus.BEAR,
        trend_strength=25.0,
        ma5=100.0,
        ma10=102.0,
        ma20=105.0,
        current_price=94.0,
        bias_ma5=-6.0,
        volume_status=VolumeStatus.NORMAL,
        macd_status=MACDStatus.BEARISH,
        rsi_status=RSIStatus.WEAK,
    )
    with patch("src.stock_analyzer.get_config") as get_config:
        get_config.return_value.bias_threshold = 5.0
        StockTrendAnalyzer()._generate_signal(result)

    assert result.signal_direction == "short"
    assert result.buy_signal in {BuySignal.SELL, BuySignal.STRONG_SELL}
    assert any("不宜追空" in item or "等待反弹" in item for item in result.risk_factors)
