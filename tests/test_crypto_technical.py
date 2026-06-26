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
    assert context["price_action"]["state"] in {
        "breakout",
        "bullish_push",
        "liquidity_sweep_high",
        "range",
    }
    assert set(context["fibonacci"]["retracement_levels"]) == {"38.2%", "50.0%", "61.8%"}
    assert context["volume"]["ratio"] is not None
    assert context["volatility"]["atr14"] is not None
    assert context["volatility"]["atr14_pct"] is not None
    assert context["vwap"]["rolling_20"] is not None
    assert context["ema"]["ema20"] is not None
    assert context["ema"]["structure"] in {"bullish", "bearish", "mixed"}


def test_build_crypto_technical_context_distinguishes_liquidity_sweep_from_breakout() -> None:
    bars = _btc_bars()
    prior_high = bars.iloc[:-1]["high"].tail(20).max()
    last_idx = bars.index[-1]
    bars.loc[last_idx, "open"] = prior_high - 400
    bars.loc[last_idx, "high"] = prior_high + 1200
    bars.loc[last_idx, "low"] = prior_high - 1600
    bars.loc[last_idx, "close"] = prior_high - 250

    context = build_crypto_technical_context(bars, "BTC")

    assert context is not None
    assert context["price_action"]["state"] == "liquidity_sweep_high"
    assert context["price_action"]["high_swept"] is True
    assert context["price_action"]["close_above_resistance"] is False


def test_build_crypto_technical_context_flags_selloff_rebound_candidate() -> None:
    bars = _btc_bars()
    bars[["open", "high", "low", "close", "volume"]] = bars[["open", "high", "low", "close", "volume"]].astype(float)
    last_idx = bars.index[-1]
    reference_high = bars.iloc[-7:-1]["high"].max()
    event_low = reference_high * 0.94
    bars.loc[last_idx, "open"] = reference_high * 0.965
    bars.loc[last_idx, "high"] = reference_high * 0.972
    bars.loc[last_idx, "low"] = event_low
    bars.loc[last_idx, "close"] = event_low * 1.012
    bars.loc[last_idx, "volume"] = bars.iloc[-21:-1]["volume"].mean() * 2

    context = build_crypto_technical_context(bars, "BTC")

    assert context is not None
    assert context["event"]["type"] in {
        "selloff_rebound_candidate",
        "liquidity_sweep_low_reversal_candidate",
    }
    assert context["event"]["urgency"] == "high"
    assert context["event"]["trigger_reference"]["long_confirmation_price"] is not None
    assert context["event"]["trigger_reference"]["long_invalidation_price"] is not None
    assert context["event"]["trigger_reference"]["short_breakdown_price"] is not None


def test_build_crypto_technical_context_ignores_regular_stocks() -> None:
    assert build_crypto_technical_context(_btc_bars(), "AAPL") is None
