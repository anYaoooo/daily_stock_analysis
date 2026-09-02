# -*- coding: utf-8 -*-
"""Closed-bar, volatility-normalized trend model for Bitcoin.

The legacy ``StockTrendAnalyzer`` is intentionally retained for equities. BTC
uses this model because a stock-style one-way buy score makes oversold and
low-volume pullbacks add *buy* points even while price is falling.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from src.crypto_technical import _split_closed_bars, build_crypto_technical_context
from src.stock_analyzer import (
    BuySignal,
    MACDStatus,
    RSIStatus,
    SignalDirection,
    PHASE_THRESHOLD_MULTIPLIERS,
    TrendAnalysisResult,
    TrendStatus,
    VOLATILITY_THRESHOLD_MULTIPLIERS,
    VolatilityState,
    VolumeStatus,
)


_MIN_BARS = 20
_PRICE_WINDOW = 20
_ATR_PERIOD = 14
_DIRECTION_WEIGHTS = {
    "price_structure": 0.45,
    "ema_structure": 0.30,
    "momentum": 0.15,
    "price_action": 0.10,
}

_PHASE_MULTIPLIERS = PHASE_THRESHOLD_MULTIPLIERS
_VOLATILITY_MULTIPLIERS = VOLATILITY_THRESHOLD_MULTIPLIERS


def _float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _clip(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return float(np.clip(value, low, high))


def _pct(value: Optional[float], base: Optional[float]) -> float:
    if value is None or base is None or base <= 0:
        return 0.0
    return (value / base - 1.0) * 100.0


class BtcTrendAnalyzer:
    """Deterministic BTC baseline used by both pipeline and agent tools."""

    def analyze(
        self,
        df: pd.DataFrame,
        code: str,
        market_phase_context: Any = None,
        market_phase: Any = None,
    ) -> TrendAnalysisResult:
        result = TrendAnalysisResult(code=code, signal_method="btc_direction_v2")
        result.market_phase = self._normalize_phase(
            market_phase_context if market_phase_context is not None else market_phase
        )
        bars = self._closed_bars(df)
        if len(bars) < _MIN_BARS:
            result.risk_factors.append("BTC 已闭合 K 线不足，无法完成方向判断")
            return result

        latest = bars.iloc[-1]
        close = _float(latest.get("close"))
        if close is None or close <= 0:
            result.risk_factors.append("BTC 收盘价无效，无法完成方向判断")
            return result

        sma5 = _float(bars["close"].rolling(5).mean().iloc[-1]) or close
        sma10 = _float(bars["close"].rolling(10).mean().iloc[-1]) or close
        sma20 = _float(bars["close"].rolling(20).mean().iloc[-1]) or close
        sma60 = _float(bars["close"].rolling(60).mean().iloc[-1])
        ema20_series = bars["close"].ewm(span=20, adjust=False).mean()
        ema50_series = bars["close"].ewm(span=50, adjust=False).mean()
        ema20 = _float(ema20_series.iloc[-1]) or sma20
        ema50 = _float(ema50_series.iloc[-1]) or ema20

        atr_series = self._true_range(bars).rolling(_ATR_PERIOD).mean()
        atr = _float(atr_series.iloc[-1])
        atr_pct = atr / close if atr and atr > 0 else 0.0
        result.volatility_pct = round(atr_pct * 100.0, 4)
        result.volatility_state = self._volatility_state(atr_pct).value

        # Keep the trend result and the richer crypto context on one direction
        # calculation.  Both are consumed by different callers, so allowing
        # them to use separate thresholds would make the report internally
        # contradictory even when every input candle is identical.
        crypto_context = build_crypto_technical_context(df, code, lookback=max(60, len(bars)))
        direction_snapshot = (
            crypto_context.get("direction")
            if isinstance(crypto_context, dict)
            and isinstance(crypto_context.get("direction"), dict)
            else None
        )

        price_action_state = ""
        if isinstance(crypto_context, dict):
            price_action_state = str(
                (crypto_context.get("price_action") or {}).get("state") or ""
            ).lower()
        action_component = {
            "breakout": 1.0,
            "bullish_push": 0.5,
            "liquidity_sweep_low": 0.25,
            "breakdown": -1.0,
            "bearish_push": -0.5,
            "liquidity_sweep_high": -0.25,
        }.get(price_action_state, 0.0)

        fallback_price = self._price_structure(bars, atr_pct)
        fallback_ema = self._ema_structure(close, ema20, ema50, atr_pct)
        fallback_momentum = self._momentum(bars, atr_pct)
        snapshot_components = (
            direction_snapshot.get("components")
            if isinstance(direction_snapshot, dict)
            and isinstance(direction_snapshot.get("components"), dict)
            else {}
        )
        snapshot_score = _float(direction_snapshot.get("score")) if direction_snapshot else None
        if snapshot_score is not None and snapshot_components:
            components = {
                key: round(_clip(_float(snapshot_components.get(key)) or 0.0), 4)
                for key in _DIRECTION_WEIGHTS
            }
            direction_score = _clip(snapshot_score)
            price = {
                "component": components["price_structure"],
                "slope_pct": _float(direction_snapshot.get("price_slope_pct")) or 0.0,
                "return_pct": _float(direction_snapshot.get("price_return_20_pct")) or 0.0,
                "range_pct": _float(direction_snapshot.get("price_range_pct")) or 0.0,
                "efficiency": _float(direction_snapshot.get("directional_efficiency")) or 0.0,
                "trend": str(direction_snapshot.get("trend") or "震荡"),
            }
            ema = {
                "component": components["ema_structure"],
                "close_vs_ema_pct": (close / ema20 - 1.0) * 100.0 if ema20 > 0 else 0.0,
            }
            momentum = components["momentum"]
        else:
            # A compact OHLCV frame without timestamps cannot build the full
            # crypto context. Preserve a deterministic local fallback for
            # callers that only need the legacy result shape.
            price = fallback_price
            ema = fallback_ema
            momentum = fallback_momentum
            components = {
                "price_structure": round(price["component"], 4),
                "ema_structure": round(ema["component"], 4),
                "momentum": round(momentum, 4),
                "price_action": round(action_component, 4),
            }
            direction_score = _clip(
                sum(_DIRECTION_WEIGHTS[key] * components[key] for key in _DIRECTION_WEIGHTS)
            )
        result.direction_score = round(direction_score, 4)
        result.signal_components = {
            **components,
            "price_structure_weight": _DIRECTION_WEIGHTS["price_structure"],
            "ema_structure_weight": _DIRECTION_WEIGHTS["ema_structure"],
            "momentum_weight": _DIRECTION_WEIGHTS["momentum"],
            "price_action_weight": _DIRECTION_WEIGHTS["price_action"],
            "atr_pct": round(atr_pct * 100.0, 4),
            "price_return_20_pct": round(price["return_pct"], 4),
            "threshold_pct": _float(direction_snapshot.get("threshold_pct")) if direction_snapshot else None,
            "period": direction_snapshot.get("period") if direction_snapshot else None,
        }

        result.current_price = close
        result.ma5 = sma5
        result.ma10 = sma10
        result.ma20 = sma20
        result.ma60 = sma60 if sma60 is not None else sma20
        result.bias_ma5 = _pct(close, sma5)
        result.bias_ma10 = _pct(close, sma10)
        result.bias_ma20 = _pct(close, sma20)
        result.price_trend = price["trend"]
        result.price_slope_pct = round(price["slope_pct"], 4)
        result.price_return_pct = round(price["return_pct"], 4)
        result.price_range_pct = round(price["range_pct"], 4)
        result.directional_efficiency = round(price["efficiency"], 4)
        result.price_structure_available = True
        result.trend_status, result.ma_alignment = self._trend_status(direction_score, ema)
        result.trend_strength = round(50.0 + abs(direction_score) * 50.0, 2)

        direction = (
            SignalDirection.LONG.value
            if direction_score > 0
            else SignalDirection.SHORT.value
            if direction_score < 0
            else SignalDirection.NEUTRAL.value
        )
        result.market_regime = (
            "bull_trend" if direction_score >= 0.20
            else "bear_trend" if direction_score <= -0.20
            else "range"
        )
        result.threshold_profile = self.calibrate_thresholds(
            market_phase=result.market_phase,
            volatility_state=result.volatility_state,
            direction=direction,
            market_regime=result.market_regime,
        )
        result.signal_direction = direction

        result.volume_status, result.volume_ratio_5d, result.volume_trend = self._volume(bars)
        self._levels(bars, result, atr_pct)
        self._macd(bars, result)
        self._rsi(bars, result)

        result.signal_score = int(round(50.0 + direction_score * 50.0))
        profile = result.threshold_profile
        if direction_score >= profile["long_strong_score"]:
            result.buy_signal = BuySignal.STRONG_BUY
        elif direction_score >= profile["long_entry_score"]:
            result.buy_signal = BuySignal.BUY
        elif direction_score <= -profile["short_strong_score"]:
            result.buy_signal = BuySignal.STRONG_SELL
        elif direction_score <= -profile["short_entry_score"]:
            result.buy_signal = BuySignal.SELL
        else:
            result.buy_signal = BuySignal.WAIT

        result.signal_reasons = [
            f"BTC 对称方向分数={direction_score:+.2f}（0 为中性，不代表买入分）",
            f"价格结构={price['component']:+.2f}，20根收盘变化={price['return_pct']:+.2f}%",
            f"EMA结构={ema['component']:+.2f}，价格相对EMA20={ema['close_vs_ema_pct']:+.2f}%",
            f"动量={momentum:+.2f}，价格行为={price_action_state or '无明确突破/跌破'}",
        ]
        if atr_pct >= 0.06:
            result.risk_factors.append(f"ATR14 约为价格的 {atr_pct:.1%}，波动过高，应缩小仓位")
        if result.rsi_status in {RSIStatus.OVERBOUGHT, RSIStatus.OVERSOLD}:
            result.risk_factors.append(f"RSI 处于{result.rsi_status.value}，只作为风险提示，不单独反转方向")
        if abs(direction_score) < min(profile["long_entry_score"], profile["short_entry_score"]):
            result.risk_factors.append("多空证据未达到方向门槛，保持观望而不是强行交易")
        if crypto_context and isinstance(crypto_context.get("live_partial_bar"), dict):
            result.risk_factors.append("实时未收线 K 线未参与指标和方向计算")
        return result

    @staticmethod
    def _normalize_phase(value: Any) -> str:
        if isinstance(value, dict):
            value = value.get("phase")
        elif hasattr(value, "phase"):
            value = getattr(value, "phase")
        if hasattr(value, "value"):
            value = value.value
        phase = str(value or "unknown").strip().lower().replace("-", "_")
        phase = {
            "pre_market": "premarket",
            "post_market": "postmarket",
            "close": "closing_auction",
            "closing": "closing_auction",
            "open": "intraday",
        }.get(phase, phase)
        return phase if phase in _PHASE_MULTIPLIERS else "unknown"

    @staticmethod
    def _volatility_state(atr_pct: float) -> VolatilityState:
        """Classify ATR percentage without using it as a direction vote."""
        if atr_pct <= 0.01:
            return VolatilityState.LOW
        if atr_pct <= 0.03:
            return VolatilityState.NORMAL
        if atr_pct <= 0.06:
            return VolatilityState.HIGH
        return VolatilityState.EXTREME

    @staticmethod
    def calibrate_thresholds(
        market_phase: Any = "unknown",
        volatility_state: Any = VolatilityState.NORMAL.value,
        direction: Any = SignalDirection.NEUTRAL.value,
        market_regime: str = "unknown",
        market_stage: Any = None,
        volatility: Any = None,
    ) -> Dict[str, float | str]:
        """Build state-aware long/short direction gates.

        The default profile is exactly the historical +/-0.45 and +/-0.70
        gates. State multipliers only make a gate stricter in uncertain
        sessions or volatile regimes; they never add another vote.
        """
        phase = BtcTrendAnalyzer._normalize_phase(market_phase)
        volatility_state = volatility if volatility is not None else volatility_state
        if hasattr(volatility_state, "value"):
            volatility_state = volatility_state.value
        volatility = str(volatility_state or VolatilityState.NORMAL.value).strip().lower().replace("-", "_")
        volatility = {
            "compressed": VolatilityState.LOW.value,
            "elevated": VolatilityState.HIGH.value,
            "very_high": VolatilityState.EXTREME.value,
        }.get(volatility, volatility)
        if volatility not in _VOLATILITY_MULTIPLIERS:
            volatility = VolatilityState.NORMAL.value
        if hasattr(direction, "value"):
            direction = direction.value
        direction = str(direction or SignalDirection.NEUTRAL.value).strip().lower()
        direction = {
            "bullish": SignalDirection.LONG.value,
            "bearish": SignalDirection.SHORT.value,
            "up": SignalDirection.LONG.value,
            "down": SignalDirection.SHORT.value,
        }.get(direction, direction)
        if direction not in {item.value for item in SignalDirection}:
            direction = SignalDirection.NEUTRAL.value
        regime = str(market_stage if market_stage is not None else market_regime or "unknown").strip().lower()
        regime = {
            "bull": "bull_trend",
            "bear": "bear_trend",
            "sideways": "range",
            "consolidation": "range",
        }.get(regime, regime)
        if regime not in {"bull_trend", "bear_trend", "range", "unknown"}:
            regime = "unknown"

        base = _PHASE_MULTIPLIERS[phase] * _VOLATILITY_MULTIPLIERS[volatility]
        # A side that is counter to the measured regime needs more evidence;
        # the aligned side keeps the base gate. Unknown/range stays symmetric.
        long_side = 1.0
        short_side = 1.0
        if regime == "bull_trend":
            short_side = 1.10
        elif regime == "bear_trend":
            long_side = 1.10
        elif regime == "range":
            long_side = short_side = 1.08
        if direction == SignalDirection.LONG.value:
            short_side *= 1.05
        elif direction == SignalDirection.SHORT.value:
            long_side *= 1.05

        long_entry = float(np.clip(0.45 * base * long_side, 0.35, 0.80))
        short_entry = float(np.clip(0.45 * base * short_side, 0.35, 0.80))
        long_strong = float(np.clip(0.70 * base * long_side, 0.55, 0.95))
        short_strong = float(np.clip(0.70 * base * short_side, 0.55, 0.95))
        return {
            "market_phase": phase,
            "market_regime": regime,
            "market_stage": {
                "bull_trend": "bull",
                "bear_trend": "bear",
                "range": "range",
                "unknown": "unknown",
            }[regime],
            "volatility_state": volatility,
            "direction": direction,
            "phase_multiplier": round(float(_PHASE_MULTIPLIERS[phase]), 4),
            "volatility_multiplier": round(float(_VOLATILITY_MULTIPLIERS[volatility]), 4),
            "regime_long_multiplier": round(float(long_side), 4),
            "regime_short_multiplier": round(float(short_side), 4),
            "long_entry_score": round(long_entry, 4),
            "long_strong_score": round(long_strong, 4),
            "short_entry_score": round(short_entry, 4),
            "short_strong_score": round(short_strong, 4),
        }

    @staticmethod
    def _closed_bars(df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame()
        required = {"date", "open", "high", "low", "close", "volume"}
        if not required.issubset(df.columns):
            return pd.DataFrame()
        closed, _partial, _metadata = _split_closed_bars(df, as_of=None)
        if closed.empty:
            return pd.DataFrame()
        bars = closed.copy()
        for column in ("open", "high", "low", "close", "volume"):
            bars[column] = pd.to_numeric(bars[column], errors="coerce")
        bars = bars.dropna(subset=["high", "low", "close"])
        return bars.sort_values("date").reset_index(drop=True)

    @staticmethod
    def _true_range(bars: pd.DataFrame) -> pd.Series:
        previous = bars["close"].shift(1)
        return pd.concat(
            [
                bars["high"] - bars["low"],
                (bars["high"] - previous).abs(),
                (bars["low"] - previous).abs(),
            ],
            axis=1,
        ).max(axis=1)

    @staticmethod
    def _price_structure(bars: pd.DataFrame, atr_pct: float) -> Dict[str, float | str]:
        values = bars["close"].tail(_PRICE_WINDOW).to_numpy(dtype=float)
        first = float(values[0])
        last = float(values[-1])
        x = np.arange(len(values), dtype=float)
        slope = float(np.polyfit(x, values, 1)[0]) if len(values) >= 2 else 0.0
        mean = max(float(np.mean(values)), 1e-12)
        slope_pct = slope * (len(values) - 1) / mean * 100.0
        return_pct = (last / first - 1.0) * 100.0 if first > 0 else 0.0
        range_pct = (float(np.max(values)) / first - float(np.min(values)) / first) * 100.0 if first > 0 else 0.0
        path = float(np.abs(np.diff(values)).sum())
        efficiency = abs(last - first) / path if path > 0 else 0.0
        threshold_pct = max(4.0, atr_pct * 150.0)
        component = _clip(return_pct / threshold_pct) if threshold_pct > 0 else 0.0
        trend = "震荡"
        if abs(return_pct) >= threshold_pct and efficiency >= 0.35:
            trend = "上涨" if return_pct > 0 else "下跌"
        return {
            "component": component,
            "slope_pct": slope_pct,
            "return_pct": return_pct,
            "range_pct": range_pct,
            "efficiency": float(np.clip(efficiency, 0.0, 1.0)),
            "trend": trend,
        }

    @staticmethod
    def _ema_structure(close: float, ema20: float, ema50: float, atr_pct: float) -> Dict[str, float | str]:
        scale = max(atr_pct * 1.5, 0.005)
        close_component = _clip((close / ema20 - 1.0) / scale)
        spread_component = _clip((ema20 / ema50 - 1.0) / scale) if ema50 > 0 else 0.0
        component = (close_component + spread_component) / 2.0
        return {
            "component": component,
            "close_vs_ema_pct": (close / ema20 - 1.0) * 100.0 if ema20 > 0 else 0.0,
            "structure": "bullish" if component >= 0.35 else "bearish" if component <= -0.35 else "mixed",
        }

    @staticmethod
    def _momentum(bars: pd.DataFrame, atr_pct: float) -> float:
        closes = bars["close"]
        if len(closes) < 6 or closes.iloc[-6] <= 0:
            return 0.0
        return _clip((closes.iloc[-1] / closes.iloc[-6] - 1.0) / max(atr_pct * np.sqrt(5.0), 0.015))

    @staticmethod
    def _trend_status(direction: float, ema: Dict[str, float | str]) -> tuple[TrendStatus, str]:
        if direction >= 0.70:
            return TrendStatus.STRONG_BULL, "BTC 多头趋势（价格/EMA/动量共振）"
        if direction >= 0.45:
            return TrendStatus.BULL, "BTC 偏多，等待回踩或突破确认"
        if direction >= 0.20:
            return TrendStatus.WEAK_BULL, "BTC 弱势偏多，证据仍不完整"
        if direction <= -0.70:
            return TrendStatus.STRONG_BEAR, "BTC 空头趋势（价格/EMA/动量共振）"
        if direction <= -0.45:
            return TrendStatus.BEAR, "BTC 偏空，反弹不破再确认"
        if direction <= -0.20:
            return TrendStatus.WEAK_BEAR, "BTC 弱势偏空，证据仍不完整"
        return TrendStatus.CONSOLIDATION, "BTC 多空证据接近，区间震荡"

    @staticmethod
    def _volume(bars: pd.DataFrame) -> tuple[VolumeStatus, float, str]:
        if "volume" not in bars.columns:
            return VolumeStatus.NORMAL, 0.0, "成交量缺失，仅供参考"
        previous = bars["volume"].iloc[-21:-1] if len(bars) >= 21 else bars["volume"].iloc[:-1]
        baseline = float(previous.median()) if not previous.empty else 0.0
        latest = _float(bars["volume"].iloc[-1]) or 0.0
        ratio = latest / baseline if baseline > 0 else 0.0
        change = _pct(_float(bars["close"].iloc[-1]), _float(bars["close"].iloc[-2])) if len(bars) >= 2 else 0.0
        if ratio >= 1.8:
            return (VolumeStatus.HEAVY_VOLUME_UP if change >= 0 else VolumeStatus.HEAVY_VOLUME_DOWN, ratio, "放量，作为突破/跌破确认而非单独方向依据")
        if ratio <= 0.6:
            return (VolumeStatus.SHRINK_VOLUME_UP if change >= 0 else VolumeStatus.SHRINK_VOLUME_DOWN, ratio, "缩量，不能推断主力洗盘")
        return VolumeStatus.NORMAL, ratio, "量能正常，仅作为确认因子"

    @staticmethod
    def _levels(bars: pd.DataFrame, result: TrendAnalysisResult, atr_pct: float) -> None:
        current = result.current_price
        previous = bars.iloc[:-1].tail(20) if len(bars) > 1 else bars
        high = _float(previous["high"].max()) if not previous.empty else None
        low = _float(previous["low"].min()) if not previous.empty else None
        if low is not None and low < current:
            result.support_levels.append(low)
        if high is not None and high > current:
            result.resistance_levels.append(high)
        tolerance = max(atr_pct, 0.005)
        for label, value in (("ma5", result.ma5), ("ma10", result.ma10), ("ma20", result.ma20)):
            if value <= 0 or abs(current / value - 1.0) > tolerance:
                continue
            if current >= value:
                result.support_levels.append(value)
                if label == "ma5":
                    result.support_ma5 = True
                if label == "ma10":
                    result.support_ma10 = True
        result.support_levels = sorted({round(float(value), 2) for value in result.support_levels}, reverse=True)
        result.resistance_levels = sorted({round(float(value), 2) for value in result.resistance_levels})

    @staticmethod
    def _macd(bars: pd.DataFrame, result: TrendAnalysisResult) -> None:
        dif_series = bars["close"].ewm(span=12, adjust=False).mean() - bars["close"].ewm(span=26, adjust=False).mean()
        dea_series = dif_series.ewm(span=9, adjust=False).mean()
        dif = _float(dif_series.iloc[-1]) or 0.0
        dea = _float(dea_series.iloc[-1]) or 0.0
        result.macd_dif, result.macd_dea, result.macd_bar = dif, dea, (dif - dea) * 2.0
        prev_gap = float(dif_series.iloc[-2] - dea_series.iloc[-2]) if len(bars) >= 2 else 0.0
        gap = dif - dea
        if prev_gap <= 0 < gap and dif > 0:
            result.macd_status, result.macd_signal = MACDStatus.GOLDEN_CROSS_ZERO, "零轴上金叉"
        elif prev_gap <= 0 < gap:
            result.macd_status, result.macd_signal = MACDStatus.GOLDEN_CROSS, "金叉"
        elif prev_gap >= 0 > gap:
            result.macd_status, result.macd_signal = MACDStatus.DEATH_CROSS, "死叉"
        elif dif > 0 and dea > 0:
            result.macd_status, result.macd_signal = MACDStatus.BULLISH, "MACD 位于零轴上方"
        elif dif < 0 and dea < 0:
            result.macd_status, result.macd_signal = MACDStatus.BEARISH, "MACD 位于零轴下方"
        else:
            result.macd_status, result.macd_signal = MACDStatus.BULLISH, "MACD 中性区域"

    @staticmethod
    def _rsi(bars: pd.DataFrame, result: TrendAnalysisResult) -> None:
        delta = bars["close"].diff()
        values = {}
        for period in (6, 12, 24):
            gain = delta.where(delta > 0, 0.0).ewm(alpha=1 / period, adjust=False).mean()
            loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1 / period, adjust=False).mean()
            rs = gain / loss.replace(0, np.nan)
            rsi = 100.0 - 100.0 / (1.0 + rs)
            # Boundary cases are meaningful: a one-sided run is not neutral.
            # Only a flat window (zero average gain and loss) is assigned 50.
            only_up = (loss == 0) & (gain > 0)
            only_down = (gain == 0) & (loss > 0)
            flat = (gain == 0) & (loss == 0)
            rsi = rsi.mask(only_up, 100.0).mask(only_down, 0.0).mask(flat, 50.0)
            values[period] = rsi.fillna(50.0).iloc[-1]
        result.rsi_6, result.rsi_12, result.rsi_24 = [float(values[p]) for p in (6, 12, 24)]
        mid = result.rsi_12
        if mid > 70:
            result.rsi_status, result.rsi_signal = RSIStatus.OVERBOUGHT, f"RSI 超买({mid:.1f})，仅提示回撤风险"
        elif mid > 60:
            result.rsi_status, result.rsi_signal = RSIStatus.STRONG_BUY, f"RSI 偏强({mid:.1f})"
        elif mid >= 40:
            result.rsi_status, result.rsi_signal = RSIStatus.NEUTRAL, f"RSI 中性({mid:.1f})"
        elif mid >= 30:
            result.rsi_status, result.rsi_signal = RSIStatus.WEAK, f"RSI 偏弱({mid:.1f})"
        else:
            result.rsi_status, result.rsi_signal = RSIStatus.OVERSOLD, f"RSI 超卖({mid:.1f})，不等于立即反弹"
