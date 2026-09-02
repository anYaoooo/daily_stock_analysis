# -*- coding: utf-8 -*-
"""Regression tests for the BTC-specific symmetric direction baseline."""

from datetime import datetime, timedelta, timezone

import pandas as pd

from src.btc_trend_analyzer import BtcTrendAnalyzer
from src.crypto_technical import build_crypto_technical_context
from src.stock_analyzer import BuySignal, StockTrendAnalyzer


def _bars(closes: list[float], *, partial: bool = False) -> pd.DataFrame:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index, close in enumerate(closes):
        rows.append(
            {
                "date": start + timedelta(days=index),
                "open": close,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": 1000.0,
            }
        )
    frame = pd.DataFrame(rows)
    if partial:
        frame.attrs["period"] = "daily"
        frame.attrs["fetched_at"] = (start + timedelta(days=len(closes) - 1, hours=6)).isoformat()
    return frame


def test_btc_uses_symmetric_direction_model_for_rising_and_falling_markets() -> None:
    rising = StockTrendAnalyzer().analyze(_bars([90_000 + i * 400 for i in range(70)]), "BTC")
    falling = StockTrendAnalyzer().analyze(_bars([120_000 - i * 400 for i in range(70)]), "BTC")

    assert rising.signal_method == "btc_direction_v2"
    assert falling.signal_method == "btc_direction_v2"
    assert rising.market_regime == rising.threshold_profile["market_regime"]
    assert falling.market_regime == falling.threshold_profile["market_regime"]
    assert rising.direction_score > 0.45
    assert falling.direction_score < -0.45
    assert rising.buy_signal in {BuySignal.BUY, BuySignal.STRONG_BUY}
    assert falling.buy_signal in {BuySignal.SELL, BuySignal.STRONG_SELL}
    assert falling.signal_score < 50


def test_btc_does_not_turn_an_oversold_selloff_into_a_buy_signal() -> None:
    closes = [120_000 - i * 500 for i in range(55)]
    result = BtcTrendAnalyzer().analyze(_bars(closes), "BTC")

    assert result.rsi_status.value == "超卖"
    assert result.direction_score < 0
    assert result.buy_signal in {BuySignal.SELL, BuySignal.STRONG_SELL, BuySignal.WAIT}
    assert not (result.buy_signal in {BuySignal.BUY, BuySignal.STRONG_BUY})


def test_btc_excludes_current_unclosed_daily_bar_from_direction() -> None:
    closes = [90_000 + i * 250 for i in range(65)]
    frame = _bars(closes + [200_000], partial=True)
    result = BtcTrendAnalyzer().analyze(frame, "BTC")

    assert result.current_price == closes[-1]
    assert result.signal_method == "btc_direction_v2"
    assert any("未收线" in item for item in result.risk_factors)


def test_btc_trend_result_matches_crypto_context_direction_snapshot() -> None:
    frame = _bars([90_000 + i * 250 for i in range(70)])
    result = BtcTrendAnalyzer().analyze(frame, "BTC")
    context = build_crypto_technical_context(frame, "BTC", lookback=70)

    assert context is not None
    direction = context["direction"]
    assert result.direction_score == direction["score"]
    for key, value in direction["components"].items():
        assert result.signal_components[key] == value
