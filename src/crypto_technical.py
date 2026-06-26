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


def _pct_change(current: Optional[float], base: Optional[float]) -> Optional[float]:
    if current is None or base is None or base <= 0:
        return None
    return (current - base) / base * 100


def _build_event_context(
    bars: pd.DataFrame,
    *,
    close: float,
    recent_low: Optional[float],
    low_swept: bool,
    close_below_support: bool,
    atr14: Optional[float],
    vwap: Optional[float],
    ema20: Optional[float],
) -> Dict[str, Any]:
    """Summarize recent BTC shock moves into concrete intraday trigger references."""
    prior = bars.iloc[:-1] if len(bars) > 1 else bars
    reference_window = prior.tail(12)
    event_window = bars.tail(6)

    reference_high = _safe_float(reference_window["high"].max()) if not reference_window.empty else None
    event_low = _safe_float(event_window["low"].min()) if not event_window.empty else None
    event_low_bar = event_window.loc[event_window["low"].idxmin()] if event_low is not None else None
    event_bar_high = _safe_float(event_low_bar.get("high")) if event_low_bar is not None else None

    drop_from_reference_high_pct = _pct_change(event_low, reference_high)
    rebound_from_event_low_pct = _pct_change(close, event_low)
    atr_move = None
    if reference_high is not None and event_low is not None and atr14 and atr14 > 0:
        atr_move = (reference_high - event_low) / atr14

    shock_move = bool(
        (drop_from_reference_high_pct is not None and drop_from_reference_high_pct <= -3.0)
        or (atr_move is not None and atr_move >= 2.0)
    )
    reclaim_candidates = [value for value in (event_bar_high, vwap, ema20) if value is not None and value > close]
    confirmation_price = min(reclaim_candidates) if reclaim_candidates else event_bar_high
    invalidation_buffer = atr14 * 0.3 if atr14 is not None and atr14 > 0 else None
    long_invalidation = (event_low - invalidation_buffer) if event_low is not None and invalidation_buffer is not None else event_low
    short_breakdown = recent_low if recent_low is not None else event_low

    event_type = "none"
    suggested_direction = "wait"
    urgency = "normal"
    interpretation = "未检测到足够明确的急跌、扫低或反转事件。"

    if low_swept and not close_below_support:
        event_type = "liquidity_sweep_low_reversal_candidate"
        suggested_direction = "conditional_long"
        urgency = "high"
        interpretation = "插针扫过前低后未收盘跌破，优先按假跌破/反弹候选处理，等待右侧确认。"
    elif shock_move:
        urgency = "high"
        if confirmation_price is not None and close >= confirmation_price:
            event_type = "selloff_rebound_confirmed"
            suggested_direction = "long"
            interpretation = "急跌后已收回关键确认价，允许评估日内反弹多单，但必须使用事件低点下方作为失效边界。"
        elif rebound_from_event_low_pct is not None and rebound_from_event_low_pct >= 0.5:
            event_type = "selloff_rebound_candidate"
            suggested_direction = "conditional_long"
            interpretation = "急跌后从低点反弹，但尚未完全确认，需等待确认价触发，避免左侧抄底。"
        else:
            event_type = "sharp_selloff_wait_reclaim"
            suggested_direction = "wait"
            interpretation = "急跌仍未出现足够右侧确认，不应直接抄底；必须同时给出上破确认与下破延续两套条件。"
    elif close_below_support:
        event_type = "breakdown_continuation"
        suggested_direction = "conditional_short"
        urgency = "high"
        interpretation = "收盘跌破前低/支撑，优先按跌破延续处理，反抽不过确认价时偏空。"

    return {
        "type": event_type,
        "suggested_direction": suggested_direction,
        "urgency": urgency,
        "reference_high": _round(reference_high),
        "event_low": _round(event_low),
        "event_bar_high": _round(event_bar_high),
        "drop_from_reference_high_pct": _round(drop_from_reference_high_pct, 2),
        "rebound_from_event_low_pct": _round(rebound_from_event_low_pct, 2),
        "atr_move": _round(atr_move, 2),
        "trigger_reference": {
            "long_confirmation_price": _round(confirmation_price),
            "long_invalidation_price": _round(long_invalidation),
            "short_breakdown_price": _round(short_breakdown),
            "stop_buffer_atr": _round(invalidation_buffer),
        },
        "interpretation": interpretation,
    }


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

    prev_close_series = bars["close"].shift(1)
    true_range = pd.concat(
        [
            bars["high"] - bars["low"],
            (bars["high"] - prev_close_series).abs(),
            (bars["low"] - prev_close_series).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr14 = _safe_float(true_range.tail(14).mean()) if len(true_range.dropna()) >= 14 else None
    atr_pct = (atr14 / close * 100) if atr14 is not None and close > 0 else None

    prior_bars = bars.iloc[:-1] if len(bars) > 1 else bars
    recent_high = _safe_float(prior_bars["high"].tail(20).max())
    recent_low = _safe_float(prior_bars["low"].tail(20).min())
    body_high = max(_safe_float(latest.get("open")) or close, close)
    body_low = min(_safe_float(latest.get("open")) or close, close)
    close_change_pct = None
    if prev_close and prev_close > 0:
        close_change_pct = (close - prev_close) / prev_close * 100

    high_swept = recent_high is not None and high > recent_high
    low_swept = recent_low is not None and low < recent_low
    close_above_resistance = recent_high is not None and close > recent_high
    close_below_support = recent_low is not None and close < recent_low

    price_action = "range"
    if close_above_resistance:
        price_action = "breakout"
    elif close_below_support:
        price_action = "breakdown"
    elif high_swept and not close_above_resistance:
        price_action = "liquidity_sweep_high"
    elif low_swept and not close_below_support:
        price_action = "liquidity_sweep_low"
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

    event_context = _build_event_context(
        bars,
        close=close,
        recent_low=recent_low,
        low_swept=bool(low_swept),
        close_below_support=bool(close_below_support),
        atr14=atr14,
        vwap=vwap,
        ema20=ema20,
    )

    return {
        "framework": "Price Action + Fibonacci + Volume + VWAP + EMA",
        "lookback_bars": int(len(bars)),
        "price_action": {
            "state": price_action,
            "recent_high": _round(recent_high),
            "recent_low": _round(recent_low),
            "close_change_pct": _round(close_change_pct, 2),
            "high_swept": bool(high_swept),
            "low_swept": bool(low_swept),
            "close_above_resistance": bool(close_above_resistance),
            "close_below_support": bool(close_below_support),
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
        "volatility": {
            "atr14": _round(atr14),
            "atr14_pct": _round(atr_pct, 2),
            "stop_loss_guidance": "止损距离应避开日常 ATR 噪音，除非是明确的超短线计划。",
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
        "event": event_context,
    }


def _infer_bias(context: Optional[Dict[str, Any]]) -> str:
    if not isinstance(context, dict):
        return "neutral"

    ema_structure = ((context.get("ema") or {}).get("structure") or "").strip().lower()
    vwap_position = ((context.get("vwap") or {}).get("price_position") or "").strip().lower()
    price_action = ((context.get("price_action") or {}).get("state") or "").strip().lower()

    bullish_score = 0
    bearish_score = 0
    if ema_structure == "bullish":
        bullish_score += 2
    elif ema_structure == "bearish":
        bearish_score += 2

    if vwap_position == "above":
        bullish_score += 1
    elif vwap_position == "below":
        bearish_score += 1

    if price_action in {"breakout", "bullish_push"}:
        bullish_score += 1
    elif price_action in {"breakdown", "bearish_push"}:
        bearish_score += 1

    if bullish_score > bearish_score:
        return "long"
    if bearish_score > bullish_score:
        return "short"
    return "neutral"


def _alignment(daily_bias: str, hourly_bias: str) -> str:
    if daily_bias in {"long", "short"} and hourly_bias == daily_bias:
        return f"aligned_{daily_bias}"
    if daily_bias == "short" and hourly_bias == "long":
        return "countertrend_long"
    if daily_bias == "long" and hourly_bias == "short":
        return "countertrend_short"
    if daily_bias in {"long", "short"}:
        return f"wait_for_{daily_bias}_trigger"
    if hourly_bias in {"long", "short"}:
        return "hourly_only_wait_daily_confirmation"
    return "neutral"


def _opportunity_text(alignment: str) -> str:
    if alignment == "aligned_long":
        return "小时线与日线偏多共振，可寻找顺日线的日内多单触发；止损与仓位必须受日线失效位约束。"
    if alignment == "aligned_short":
        return "小时线与日线偏空共振，可寻找顺日线的日内空单触发；止损与仓位必须受日线失效位约束。"
    if alignment == "countertrend_long":
        return "日线偏空但小时线偏多，可评估逆日线短线多单机会；必须轻仓、严格止损、限定日内有效期，不能升级为日线反转。"
    if alignment == "countertrend_short":
        return "日线偏多但小时线偏空，可评估逆日线短线空单机会；必须轻仓、严格止损、限定日内有效期，不能升级为日线反转。"
    if alignment == "hourly_only_wait_daily_confirmation":
        return "小时线有短线信号但缺少明确日线方向，可作为独立日内机会评估，需降低仓位并写清触发与失效条件。"
    if alignment.startswith("wait_for_"):
        return "日线方向存在，但小时线尚未给出同向触发，等待小时线回踩/突破/跌破确认。"
    return "多空证据不足，日内以等待或区间观察为主。"


def build_crypto_multi_timeframe_context(
    daily_df: pd.DataFrame,
    hourly_df: Optional[pd.DataFrame],
    code: str,
) -> Optional[Dict[str, Any]]:
    """Build a BTC context where the hourly layer can express independent intraday opportunities."""
    daily_context = build_crypto_technical_context(daily_df, code)
    if not daily_context:
        return None

    hourly_context = build_crypto_technical_context(hourly_df, code, lookback=72) if hourly_df is not None else None
    result = dict(daily_context)
    result["timeframes"] = {"daily": daily_context}

    daily_bias = _infer_bias(daily_context)
    hourly_bias = _infer_bias(hourly_context)
    alignment = _alignment(daily_bias, hourly_bias)

    if hourly_context:
        result["timeframes"]["hourly"] = hourly_context

    result["intraday"] = {
        "timeframe": "1h",
        "rule": "小时线作为独立日内机会层，日线提供背景、关键位和风险边界，但不一票否决小时线方向。",
        "daily_bias": daily_bias,
        "hourly_bias": hourly_bias,
        "alignment": alignment,
        "opportunity": _opportunity_text(alignment),
        "notes": [
            "顺日线方向的小时线触发优先级更高；逆日线机会只能按短线/日内计划处理。",
            "小时线入场、加减仓与止损应参考日线关键支撑/阻力和失效条件，但允许给出相反方向短线机会。",
        ],
    }
    return result
