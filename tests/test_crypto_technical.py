# -*- coding: utf-8 -*-
"""Tests for BTC-specific technical framework context."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from src.crypto_technical import build_crypto_technical_context


def _btc_bars(days: int = 65) -> pd.DataFrame:
    start = date(2026, 1, 1)
    rows = []
    for idx in range(days):
        close = 90000 + idx * 250
        high = close + 800
        low = close - 700
        open_price = close - 200
        volume = 1000 + idx * 5
        rows.append(
            {
                "date": start + timedelta(days=idx),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }
        )
    return pd.DataFrame(rows)


def test_build_crypto_technical_context_adds_requested_framework() -> None:
    context = build_crypto_technical_context(_btc_bars(), "BTC")

    assert context is not None
    assert context["framework"] == "Price Action + Fibonacci + Volume + VWAP + EMA"
    assert context["price_action"]["state"] in {"breakout", "bullish_push", "range"}
    assert set(context["fibonacci"]["retracement_levels"]) == {"38.2%", "50.0%", "61.8%"}
    assert context["volume"]["ratio"] is not None
    assert context["vwap"]["rolling_20"] is not None
    assert context["ema"]["ema20"] is not None
    assert context["ema"]["structure"] in {"bullish", "bearish", "mixed"}


def test_build_crypto_technical_context_ignores_regular_stocks() -> None:
    assert build_crypto_technical_context(_btc_bars(), "AAPL") is None
