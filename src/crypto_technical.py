# -*- coding: utf-8 -*-
"""BTC-specific technical framework signals for analysis prompts."""

from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd

_SUPPORTED_CRYPTO_CODES = {"BTC", "BTCUSDT", "BTC-USD", "BTC/USD"}


def _is_supported_crypto_code(code: str) -> bool:
    return (code or "").strip().upper() in _SUPPORTED_CRYPTO_CODES


def _safe_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(number):
        return None
    return number


def _round(value: Optional[float], digits: int = 2) -> Optional[float]:
    if value is None:
        return None
    return round(value, digits)


def build_crypto_technical_context(
    df: pd.DataFrame,
    code: str,
    *,
    lookback: int = 60,
) -> Optional[Dict[str, Any]]:
    """Build Price Action/Fibonacci/Volume/VWAP/EMA context for supported crypto symbols."""
    if not _is_supported_crypto_code(code) or df is None or df.empty:
        return None

    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(set(df.columns)):
        return None

    bars = df.sort_values("date").tail(max(20, lookback)).copy()
    if len(bars) < 20:
        return None

    for column in ("open", "high", "low", "close", "volume"):
        bars[column] = pd.to_numeric(bars[column], errors="coerce")
    bars = bars.dropna(subset=["high", "low", "close"])
    if bars.empty:
        return None

    latest = bars.iloc[-1]
    prev = bars.iloc[-2] if len(bars) >= 2 else latest
    close = _safe_float(latest.get("close"))
    high = _safe_float(latest.get("high"))
    low = _safe_float(latest.get("low"))
    prev_close = _safe_float(prev.get("close"))
    if close is None or high is None or low is None:
        return None

    swing_high = _safe_float(bars["high"].max())
    swing_low = _safe_float(bars["low"].min())
    swing_range = (swing_high - swing_low) if swing_high is not None and swing_low is not None else None
    fib_levels: Dict[str, Optional[float]] = {}
    if swing_range and swing_range > 0 and swing_high is not None:
        for label, ratio in (
            ("38.2%", 0.382),
            ("50.0%", 0.5),
            ("61.8%", 0.618),
        ):
            fib_levels[label] = _round(swing_high - swing_range * ratio)

    ema_fast = bars["close"].ewm(span=20, adjust=False).mean()
    ema_slow = bars["close"].ewm(span=50, adjust=False).mean()
    ema20 = _safe_float(ema_fast.iloc[-1])
    ema50 = _safe_float(ema_slow.iloc[-1])

    typical_price = (bars["high"] + bars["low"] + bars["close"]) / 3
    volume = bars["volume"].fillna(0)
    volume_sum = _safe_float(volume.tail(20).sum())
    vwap = None
    if volume_sum and volume_sum > 0:
        vwap = _safe_float((typical_price.tail(20) * volume.tail(20)).sum() / volume_sum)

    avg_volume = _safe_float(volume.tail(21).iloc[:-1].mean()) if len(volume) >= 21 else _safe_float(volume.mean())
    latest_volume = _safe_float(latest.get("volume"))
    volume_ratio = None
    if avg_volume and avg_volume > 0 and latest_volume is not None:
        volume_ratio = latest_volume / avg_volume

    recent_high = _safe_float(bars["high"].tail(20).max())
    recent_low = _safe_float(bars["low"].tail(20).min())
    body_high = max(_safe_float(latest.get("open")) or close, close)
    body_low = min(_safe_float(latest.get("open")) or close, close)
    close_change_pct = None
    if prev_close and prev_close > 0:
        close_change_pct = (close - prev_close) / prev_close * 100

    price_action = "range"
    if recent_high is not None and close >= recent_high:
        price_action = "breakout"
    elif recent_low is not None and close <= recent_low:
        price_action = "breakdown"
    elif close_change_pct is not None and close_change_pct > 0 and close >= body_high:
        price_action = "bullish_push"
    elif close_change_pct is not None and close_change_pct < 0 and close <= body_low:
        price_action = "bearish_push"

    volume_confirmation = "normal"
    if volume_ratio is not None:
        if volume_ratio >= 1.5:
            volume_confirmation = "high"
        elif volume_ratio <= 0.7:
            volume_confirmation = "low"

    vwap_position = None
    if vwap is not None:
        vwap_position = "above" if close > vwap else "below" if close < vwap else "at"

    ema_structure = None
    if ema20 is not None and ema50 is not None:
        if close > ema20 > ema50:
            ema_structure = "bullish"
        elif close < ema20 < ema50:
            ema_structure = "bearish"
        else:
            ema_structure = "mixed"

    return {
        "framework": "Price Action + Fibonacci + Volume + VWAP + EMA",
        "lookback_bars": int(len(bars)),
        "price_action": {
            "state": price_action,
            "recent_high": _round(recent_high),
            "recent_low": _round(recent_low),
            "close_change_pct": _round(close_change_pct, 2),
        },
        "fibonacci": {
            "swing_high": _round(swing_high),
            "swing_low": _round(swing_low),
            "retracement_levels": fib_levels,
        },
        "volume": {
            "latest": _round(latest_volume, 4),
            "average": _round(avg_volume, 4),
            "ratio": _round(volume_ratio, 2),
            "confirmation": volume_confirmation,
        },
        "vwap": {
            "rolling_20": _round(vwap),
            "price_position": vwap_position,
        },
        "ema": {
            "ema20": _round(ema20),
            "ema50": _round(ema50),
            "structure": ema_structure,
        },
    }
