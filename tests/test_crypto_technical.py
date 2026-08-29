# -*- coding: utf-8 -*-
"""Tests for BTC-specific technical framework context."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

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
    forecast = context["volatility"]["forecast"]
    assert forecast["model_version"] == "btc-ewma-vol-v1"
    assert forecast["data_quality"] == "available"
    assert forecast["forecast_sigma_pct"] is not None
    assert forecast["regime"] in {"compressed", "normal", "elevated", "extreme"}
    assert 0.0 < forecast["position_multiplier_cap"] <= 1.0
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
    event = context["event"]
    assert event["type"] == "liquidity_sweep_high_reversal_candidate"
    assert event["suggested_direction"] == "conditional_short"
    assert event["right_side"]["version"] == "btc-right-side-v1"
    assert event["right_side"]["state"] == "sweep_detected"
    assert event["right_side"]["direction"] == "short"
    assert event["right_side"]["confirmation_price"] is not None
    assert event["right_side"]["invalidation_price"] is not None
    assert event["right_side"]["confirmation_add_requires_retest"] is True


def test_build_crypto_technical_context_caps_position_in_extreme_ewma_regime() -> None:
    bars = _btc_bars()
    bars[["open", "high", "low", "close"]] = bars[["open", "high", "low", "close"]].astype(float)
    last_idx = bars.index[-1]
    prior_close = float(bars.loc[last_idx - 1, "close"])
    bars.loc[last_idx, "open"] = prior_close
    bars.loc[last_idx, "high"] = prior_close * 1.13
    bars.loc[last_idx, "low"] = prior_close * 0.99
    bars.loc[last_idx, "close"] = prior_close * 1.12

    context = build_crypto_technical_context(bars, "BTC")

    assert context is not None
    forecast = context["volatility"]["forecast"]
    assert forecast["regime"] == "extreme"
    assert forecast["position_multiplier_cap"] == 0.25
    assert forecast["risk_action"] == "reduce_position_strongly"


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
    assert context["event"]["right_side"]["direction"] == "long"
    assert context["event"]["right_side"]["state"] == "sweep_detected"


def test_build_crypto_technical_context_ignores_regular_stocks() -> None:
    assert build_crypto_technical_context(_btc_bars(), "AAPL") is None


def test_build_crypto_technical_context_excludes_live_hourly_bar_from_indicators() -> None:
    start = datetime(2026, 7, 15, tzinfo=timezone.utc)
    rows = []
    for idx in range(65):
        rows.append(
            {
                "date": start + timedelta(hours=idx),
                "open": 63000 + idx,
                "high": 63100 + idx,
                "low": 62900 + idx,
                "close": 63050 + idx,
                "volume": 1000 + idx,
            }
        )
    bars = pd.DataFrame(rows)
    latest_open = start + timedelta(hours=64)
    bars.loc[bars.index[-1], "close"] = 62000
    bars.loc[bars.index[-1], "volume"] = 1
    bars.attrs.update(
        {
            "period": "hourly",
            "fetched_at": (latest_open + timedelta(minutes=7)).isoformat(),
        }
    )

    context = build_crypto_technical_context(bars, "BTC", lookback=72)

    assert context is not None
    assert context["lookback_bars"] == 64
    assert context["bar_state"]["closed_bar_count"] == 64
    assert context["bar_state"]["partial_bar_count"] == 1
    assert context["bar_state"]["indicators_use_closed_bars_only"] is True
    assert context["volume"]["latest"] == 1063
    assert context["volume"]["ratio"] != 0.0
    assert context["live_partial_bar"]["price"] == 62000
    assert context["live_partial_bar"]["volume"] == 1


def test_build_crypto_technical_context_excludes_current_daily_bar() -> None:
    bars = _btc_bars()
    current_day = datetime(2026, 7, 18, tzinfo=timezone.utc)
    partial = bars.iloc[-1].copy()
    partial["date"] = current_day.date()
    partial["close"] = 1
    partial["volume"] = 1
    bars = pd.concat([bars, pd.DataFrame([partial])], ignore_index=True)
    bars.attrs.update(
        {
            "period": "daily",
            "fetched_at": (current_day + timedelta(hours=14, minutes=7)).isoformat(),
        }
    )

    context = build_crypto_technical_context(bars, "BTC")

    assert context is not None
    assert context["bar_state"]["partial_bar_count"] == 1
    assert context["live_partial_bar"]["price"] == 1
    assert context["price_action"]["close_change_pct"] > 0
