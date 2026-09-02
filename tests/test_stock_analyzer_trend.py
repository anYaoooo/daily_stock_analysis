# -*- coding: utf-8 -*-
"""Regression tests for price-structure trend classification."""

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from src.stock_analyzer import StockTrendAnalyzer, TrendStatus
from src.agent.tools.analysis_tools import _handle_analyze_pattern
from src.core.pipeline import StockAnalysisPipeline


def _make_daily_frame(prices):
    start = date(2026, 1, 1)
    return pd.DataFrame(
        {
            "date": [start + timedelta(days=i) for i in range(len(prices))],
            "open": prices,
            "high": prices,
            "low": prices,
            "close": prices,
            "volume": [100] * len(prices),
        }
    )


def _analyze(prices):
    with patch("src.stock_analyzer.get_config") as get_config:
        get_config.return_value.bias_threshold = 5.0
        return StockTrendAnalyzer().analyze(_make_daily_frame(prices), "TEST")


def test_flat_price_is_consolidation_even_when_short_mas_have_tiny_ordering():
    result = _analyze([100.0 + (0.2 if i % 2 else 0.0) for i in range(60)])

    assert result.price_trend == "震荡"
    assert result.trend_status is TrendStatus.CONSOLIDATION
    assert result.directional_efficiency < 0.45
    assert result.price_structure_available is True


def test_monotonic_rise_is_directional_bullish():
    result = _analyze([100.0 + i for i in range(60)])

    assert result.price_trend == "上涨"
    assert result.trend_status in {TrendStatus.BULL, TrendStatus.STRONG_BULL}
    assert result.directional_efficiency > 0.9
    assert result.price_return_pct > 10


def test_monotonic_decline_is_directional_bearish():
    result = _analyze([160.0 - i for i in range(60)])

    assert result.price_trend == "下跌"
    assert result.trend_status in {TrendStatus.BEAR, TrendStatus.STRONG_BEAR}
    assert result.directional_efficiency > 0.9
    assert result.price_return_pct < -10


def test_to_dict_exposes_price_structure_evidence():
    result = _analyze([100.0 + i * 0.5 for i in range(60)])
    payload = result.to_dict()

    assert payload["price_trend"] == result.price_trend
    assert payload["price_slope_pct"] == result.price_slope_pct
    assert payload["directional_efficiency"] == result.directional_efficiency


def test_report_headline_uses_measured_price_direction_over_conflicting_agent_text():
    trend_result = _analyze([100.0 + i for i in range(60)])
    result = SimpleNamespace(
        trend_prediction="看空",
        dashboard={},
        data_sources="agent:test",
    )

    StockAnalysisPipeline._apply_local_trend_baseline(result, trend_result, "zh")

    assert result.trend_prediction == "看多"
    assert result.dashboard["trend_prediction"] == "看多"
    assert "trend:technical_baseline" in result.data_sources


def test_btc_report_headline_uses_symmetric_direction_gate():
    trend_result = SimpleNamespace(
        signal_method="btc_direction_v2",
        direction_score=0.52,
        price_structure_available=True,
        price_trend="震荡",
    )

    assert StockAnalysisPipeline._trend_label_fallback(trend_result, "zh") == "看多"


def test_pattern_detector_does_not_call_a_narrow_one_way_drift_a_box():
    prices = [100.0 + i * 0.5 for i in range(20)]
    frame = _make_daily_frame(prices)

    with patch("src.services.history_loader.load_history_df", return_value=(frame, "test")):
        result = _handle_analyze_pattern("TEST", days=10)

    assert "箱体震荡" not in result["summary"]


def test_pattern_detector_keeps_box_label_for_low_efficiency_price_path():
    prices = [100.0, 102.0, 98.0, 101.0, 99.0, 100.0, 102.0, 98.0, 101.0, 99.0] * 2
    frame = _make_daily_frame(prices)

    with patch("src.services.history_loader.load_history_df", return_value=(frame, "test")):
        result = _handle_analyze_pattern("TEST", days=10)

    assert "箱体震荡" in result["summary"]
