# -*- coding: utf-8 -*-
"""
===================================
趋势交易分析器 - 基于用户交易理念
===================================

交易理念核心原则：
1. 严进策略 - 不追高，追求每笔交易成功率
2. 趋势交易 - MA5>MA10>MA20 多头排列，顺势而为
3. 效率优先 - 关注筹码结构好的股票
4. 买点偏好 - 在 MA5/MA10 附近回踩买入

技术标准：
- 多头排列：MA5 > MA10 > MA20
- 乖离率：(Close - MA5) / MA5 < 5%（不追高）
- 量能形态：缩量回调优先
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, Any, List
from enum import Enum

import pandas as pd
import numpy as np

from src.config import get_config

logger = logging.getLogger(__name__)


# Shared state multipliers used by both equity and BTC analyzers. Directional
# and bias-specific factors remain local to each model because their score
# semantics differ.
PHASE_THRESHOLD_MULTIPLIERS = {
    "premarket": 1.15,
    "intraday": 1.00,
    "lunch_break": 1.20,
    "closing_auction": 1.10,
    "postmarket": 0.95,
    "non_trading": 1.25,
    "unknown": 1.00,
}
VOLATILITY_THRESHOLD_MULTIPLIERS = {
    "low": 0.85,
    "normal": 1.00,
    "high": 1.20,
    "extreme": 1.40,
}


class TrendStatus(Enum):
    """趋势状态枚举"""
    STRONG_BULL = "强势多头"      # MA5 > MA10 > MA20，且间距扩大
    BULL = "多头排列"             # MA5 > MA10 > MA20
    WEAK_BULL = "弱势多头"        # MA5 > MA10，但 MA10 < MA20
    CONSOLIDATION = "盘整"        # 均线缠绕
    WEAK_BEAR = "弱势空头"        # MA5 < MA10，但 MA10 > MA20
    BEAR = "空头排列"             # MA5 < MA10 < MA20
    STRONG_BEAR = "强势空头"      # MA5 < MA10 < MA20，且间距扩大


class VolumeStatus(Enum):
    """量能状态枚举"""
    HEAVY_VOLUME_UP = "放量上涨"       # 量价齐升
    HEAVY_VOLUME_DOWN = "放量下跌"     # 放量杀跌
    SHRINK_VOLUME_UP = "缩量上涨"      # 无量上涨
    SHRINK_VOLUME_DOWN = "缩量回调"    # 缩量回调（好）
    NORMAL = "量能正常"


class BuySignal(Enum):
    """买入信号枚举"""
    STRONG_BUY = "强烈买入"       # 多条件满足
    BUY = "买入"                  # 基本条件满足
    HOLD = "持有"                 # 已持有可继续
    WAIT = "观望"                 # 等待更好时机
    SELL = "卖出"                 # 趋势转弱
    STRONG_SELL = "强烈卖出"      # 趋势破坏


class MACDStatus(Enum):
    """MACD状态枚举"""
    GOLDEN_CROSS_ZERO = "零轴上金叉"      # DIF上穿DEA，且在零轴上方
    GOLDEN_CROSS = "金叉"                # DIF上穿DEA
    BULLISH = "多头"                    # DIF>DEA>0
    CROSSING_UP = "上穿零轴"             # DIF上穿零轴
    CROSSING_DOWN = "下穿零轴"           # DIF下穿零轴
    BEARISH = "空头"                    # DIF<DEA<0
    DEATH_CROSS = "死叉"                # DIF下穿DEA


class RSIStatus(Enum):
    """RSI状态枚举"""
    OVERBOUGHT = "超买"        # RSI > 70
    STRONG_BUY = "强势买入"    # 50 < RSI < 70
    NEUTRAL = "中性"          # 40 <= RSI <= 60
    WEAK = "弱势"             # 30 < RSI < 40
    OVERSOLD = "超卖"         # RSI < 30


class SignalDirection(Enum):
    """Directional side used by the threshold calibration layer."""

    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


class VolatilityState(Enum):
    """Realized volatility bucket used to widen or tighten thresholds."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    EXTREME = "extreme"


@dataclass
class TrendAnalysisResult:
    """趋势分析结果"""
    code: str
    
    # 趋势判断
    trend_status: TrendStatus = TrendStatus.CONSOLIDATION
    ma_alignment: str = ""           # 均线排列描述
    trend_strength: float = 0.0      # 趋势强度 0-100

    # 价格结构（用于区分真正的方向性走势与均线滞后造成的误判）
    price_trend: str = "震荡"        # 上涨 / 下跌 / 震荡
    price_slope_pct: float = 0.0      # 观察窗口内线性拟合的累计斜率（%）
    price_return_pct: float = 0.0     # 观察窗口首尾收盘价变化（%）
    price_range_pct: float = 0.0      # 观察窗口最高最低价振幅（%）
    directional_efficiency: float = 0.0  # 净位移 / 总路径，范围 0-1
    price_structure_available: bool = False
    
    # 均线数据
    ma5: float = 0.0
    ma10: float = 0.0
    ma20: float = 0.0
    ma60: float = 0.0
    current_price: float = 0.0
    
    # 乖离率（与 MA5 的偏离度）
    bias_ma5: float = 0.0            # (Close - MA5) / MA5 * 100
    bias_ma10: float = 0.0
    bias_ma20: float = 0.0
    
    # 量能分析
    volume_status: VolumeStatus = VolumeStatus.NORMAL
    volume_ratio_5d: float = 0.0     # 当日成交量/5日均量
    volume_trend: str = ""           # 量能趋势描述
    
    # 支撑压力
    support_ma5: bool = False        # MA5 是否构成支撑
    support_ma10: bool = False       # MA10 是否构成支撑
    resistance_levels: List[float] = field(default_factory=list)
    support_levels: List[float] = field(default_factory=list)

    # MACD 指标
    macd_dif: float = 0.0          # DIF 快线
    macd_dea: float = 0.0          # DEA 慢线
    macd_bar: float = 0.0           # MACD 柱状图
    macd_status: MACDStatus = MACDStatus.BULLISH
    macd_signal: str = ""            # MACD 信号描述

    # RSI 指标
    rsi_6: float = 0.0              # RSI(6) 短期
    rsi_12: float = 0.0             # RSI(12) 中期
    rsi_24: float = 0.0             # RSI(24) 长期
    rsi_status: RSIStatus = RSIStatus.NEUTRAL
    rsi_signal: str = ""              # RSI 信号描述

    # 买入信号
    buy_signal: BuySignal = BuySignal.WAIT
    signal_score: int = 0            # 综合评分 0-100
    signal_reasons: List[str] = field(default_factory=list)
    risk_factors: List[str] = field(default_factory=list)

    # BTC 专用方向评分审计字段。股票旧路径保持原有评分语义；BTC 路径
    # 使用以 0 为中轴的对称方向分数，避免把“买入吸引力”误当成涨跌概率。
    direction_score: float = 0.0     # -1=强空，0=中性，+1=强多
    signal_components: Dict[str, float] = field(default_factory=dict)
    signal_method: str = "stock_score_v1"

    # Threshold calibration audit fields. ``market_phase`` is the regular
    # session phase supplied by the pipeline; ``market_regime`` is inferred
    # from the price/MA structure and is intentionally kept separate.
    market_phase: str = "unknown"
    market_regime: str = "unknown"
    volatility_state: str = VolatilityState.NORMAL.value
    volatility_pct: float = 0.0
    threshold_profile: Dict[str, Any] = field(default_factory=dict)
    signal_direction: str = SignalDirection.NEUTRAL.value
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'code': self.code,
            'trend_status': self.trend_status.value,
            'ma_alignment': self.ma_alignment,
            'trend_strength': self.trend_strength,
            'price_trend': self.price_trend,
            'price_slope_pct': self.price_slope_pct,
            'price_return_pct': self.price_return_pct,
            'price_range_pct': self.price_range_pct,
            'directional_efficiency': self.directional_efficiency,
            'price_structure_available': self.price_structure_available,
            'ma5': self.ma5,
            'ma10': self.ma10,
            'ma20': self.ma20,
            'ma60': self.ma60,
            'current_price': self.current_price,
            'bias_ma5': self.bias_ma5,
            'bias_ma10': self.bias_ma10,
            'bias_ma20': self.bias_ma20,
            'volume_status': self.volume_status.value,
            'volume_ratio_5d': self.volume_ratio_5d,
            'volume_trend': self.volume_trend,
            'support_ma5': self.support_ma5,
            'support_ma10': self.support_ma10,
            'buy_signal': self.buy_signal.value,
            'signal_score': self.signal_score,
            'signal_reasons': self.signal_reasons,
            'risk_factors': self.risk_factors,
            'market_phase': self.market_phase,
            'market_regime': self.market_regime,
            'volatility_state': self.volatility_state,
            'volatility_pct': self.volatility_pct,
            'threshold_profile': self.threshold_profile,
            'signal_direction': self.signal_direction,
            'direction_score': self.direction_score,
            'signal_components': self.signal_components,
            'signal_method': self.signal_method,
            'macd_dif': self.macd_dif,
            'macd_dea': self.macd_dea,
            'macd_bar': self.macd_bar,
            'macd_status': self.macd_status.value,
            'macd_signal': self.macd_signal,
            'rsi_6': self.rsi_6,
            'rsi_12': self.rsi_12,
            'rsi_24': self.rsi_24,
            'rsi_status': self.rsi_status.value,
            'rsi_signal': self.rsi_signal,
        }


class StockTrendAnalyzer:
    """
    股票趋势分析器

    基于用户交易理念实现：
    1. 趋势判断 - MA5>MA10>MA20 多头排列
    2. 乖离率检测 - 不追高，偏离 MA5 超过 5% 不买
    3. 量能分析 - 偏好缩量回调
    4. 买点识别 - 回踩 MA5/MA10 支撑
    5. MACD 指标 - 趋势确认和金叉死叉信号
    6. RSI 指标 - 超买超卖判断
    """
    
    # 交易参数配置（BIAS_THRESHOLD 从 Config 读取，见 _generate_signal）
    VOLUME_SHRINK_RATIO = 0.7   # 缩量判断阈值（当日量/5日均量）
    VOLUME_HEAVY_RATIO = 1.5    # 放量判断阈值
    MA_SUPPORT_TOLERANCE = 0.02  # MA 支撑判断容忍度（2%）

    # 价格结构参数。20 个交易日能覆盖短中期走势，同时避免单日噪声主导结论。
    PRICE_TREND_LOOKBACK = 20
    PRICE_DIRECTION_THRESHOLD_PCT = 2.0
    PRICE_CONSOLIDATION_RANGE_PCT = 8.0
    PRICE_CONSOLIDATION_EFFICIENCY = 0.45
    MA_ALIGNMENT_TOLERANCE_PCT = 0.005  # 均线差异小于 0.5% 时视为基本重合

    # MACD 参数（标准12/26/9）
    MACD_FAST = 12              # 快线周期
    MACD_SLOW = 26             # 慢线周期
    MACD_SIGNAL = 9             # 信号线周期

    # RSI 参数
    RSI_SHORT = 6               # 短期RSI周期
    RSI_MID = 12               # 中期RSI周期
    RSI_LONG = 24              # 长期RSI周期
    RSI_OVERBOUGHT = 70        # 超买阈值
    RSI_OVERSOLD = 30          # 超卖阈值

    # Realized-volatility buckets (ATR percentage). These are deliberately
    # broad guardrails rather than extra indicators: each bucket selects a
    # different threshold profile in ``calibrate_thresholds``.
    VOLATILITY_LOW_PCT = 1.2
    VOLATILITY_HIGH_PCT = 2.5
    VOLATILITY_EXTREME_PCT = 4.5

    # Independent calibration multipliers. Keeping the dimensions explicit
    # makes it possible to tune one source of uncertainty without stacking
    # another indicator into the score.
    PHASE_THRESHOLD_MULTIPLIERS = PHASE_THRESHOLD_MULTIPLIERS
    VOLATILITY_THRESHOLD_MULTIPLIERS = VOLATILITY_THRESHOLD_MULTIPLIERS
    VOLATILITY_BIAS_MULTIPLIERS = {
        VolatilityState.LOW.value: 0.90,
        VolatilityState.NORMAL.value: 1.00,
        VolatilityState.HIGH.value: 0.85,
        VolatilityState.EXTREME.value: 0.70,
    }
    
    def __init__(self):
        """初始化分析器"""
        pass
    
    def analyze(
        self,
        df: pd.DataFrame,
        code: str,
        market_phase_context: Any = None,
        market_phase: Any = None,
    ) -> TrendAnalysisResult:
        """
        分析股票趋势
        
        Args:
            df: 包含 OHLCV 数据的 DataFrame
            code: 股票代码
            
        Returns:
            TrendAnalysisResult 分析结果
        """
        # BTC trades 24/7 and has materially different volatility and volume
        # semantics from equities. Keep the stock implementation stable for
        # existing callers, but route crypto through its dedicated symmetric
        # direction model before any stock-specific MA/BIAS scoring runs.
        try:
            from data_provider.crypto_fetcher import is_crypto_code
        except ImportError:
            def is_crypto_code(_code):
                return False
        if is_crypto_code(code):
            from src.btc_trend_analyzer import BtcTrendAnalyzer

            return BtcTrendAnalyzer().analyze(
                df,
                code,
                market_phase_context=market_phase_context,
                market_phase=market_phase,
            )

        result = TrendAnalysisResult(code=code)
        result.market_phase = self._normalize_market_phase(
            market_phase_context if market_phase_context is not None else market_phase
        )
        
        if df is None or df.empty or len(df) < 20:
            logger.warning(f"{code} 数据不足，无法进行趋势分析")
            result.risk_factors.append("数据不足，无法完成分析")
            return result
        
        # 确保数据按日期排序
        df = df.sort_values('date').reset_index(drop=True)
        
        # 计算均线
        df = self._calculate_mas(df)

        # 计算 MACD 和 RSI
        df = self._calculate_macd(df)
        df = self._calculate_rsi(df)

        # 获取最新数据
        latest = df.iloc[-1]
        result.current_price = float(latest['close'])
        result.ma5 = float(latest['MA5'])
        result.ma10 = float(latest['MA10'])
        result.ma20 = float(latest['MA20'])
        result.ma60 = float(latest.get('MA60', 0))

        # 1. 趋势判断
        self._analyze_trend(df, result)

        # 2. Market regime and realized volatility are state inputs for the
        # final threshold profile, not additional score components.
        result.market_regime = self._infer_market_regime(result)
        self._analyze_volatility(df, result)

        # 3. 乖离率计算
        self._calculate_bias(result)

        # 4. 量能分析
        self._analyze_volume(df, result)

        # 5. 支撑压力分析
        self._analyze_support_resistance(df, result)

        # 6. MACD 分析
        self._analyze_macd(df, result)

        # 7. RSI 分析
        self._analyze_rsi(df, result)

        # 8. 生成买入信号
        self._generate_signal(result)

        return result

    @staticmethod
    def _normalize_market_phase(value: Any) -> str:
        """Normalize a calendar context or phase label to a stable value."""
        if isinstance(value, dict):
            value = value.get("phase")
        elif hasattr(value, "phase"):
            value = getattr(value, "phase")
        if isinstance(value, Enum):
            value = value.value
        phase = str(value or "unknown").strip().lower().replace("-", "_")
        phase = {
            "pre_market": "premarket",
            "post_market": "postmarket",
            "close": "closing_auction",
            "closing": "closing_auction",
            "open": "intraday",
        }.get(phase, phase)
        return phase if phase in StockTrendAnalyzer.PHASE_THRESHOLD_MULTIPLIERS else "unknown"

    @staticmethod
    def _infer_market_regime(result: TrendAnalysisResult) -> str:
        """Map measured trend structure to a small, auditable regime set."""
        if result.trend_status in {TrendStatus.STRONG_BULL, TrendStatus.BULL, TrendStatus.WEAK_BULL}:
            return "bull_trend"
        if result.trend_status in {TrendStatus.STRONG_BEAR, TrendStatus.BEAR, TrendStatus.WEAK_BEAR}:
            return "bear_trend"
        if result.trend_status == TrendStatus.CONSOLIDATION:
            return "range"
        return "transition"

    @staticmethod
    def _infer_signal_direction(result: TrendAnalysisResult) -> str:
        """Infer the side whose threshold should be calibrated.

        A neutral/range structure stays neutral even when one oscillator is
        extreme; this prevents a single indicator from forcing a direction.
        """
        if result.trend_status in {TrendStatus.STRONG_BULL, TrendStatus.BULL, TrendStatus.WEAK_BULL}:
            return SignalDirection.LONG.value
        if result.trend_status in {TrendStatus.STRONG_BEAR, TrendStatus.BEAR, TrendStatus.WEAK_BEAR}:
            return SignalDirection.SHORT.value
        if result.price_trend == "上涨":
            return SignalDirection.LONG.value
        if result.price_trend == "下跌":
            return SignalDirection.SHORT.value
        return SignalDirection.NEUTRAL.value

    def _analyze_volatility(self, df: pd.DataFrame, result: TrendAnalysisResult) -> None:
        """Estimate realized ATR volatility and assign a broad state bucket."""
        try:
            close = pd.to_numeric(df["close"], errors="coerce")
            high = pd.to_numeric(df.get("high", close), errors="coerce")
            low = pd.to_numeric(df.get("low", close), errors="coerce")
            previous = close.shift(1)
            true_range = pd.concat(
                [high - low, (high - previous).abs(), (low - previous).abs()],
                axis=1,
            ).max(axis=1)
            atr_pct_series = (true_range / close.replace(0, np.nan) * 100).dropna()
            returns_pct = (close.pct_change() * 100).dropna()
            if atr_pct_series.empty and returns_pct.empty:
                return
            atr_pct = float(atr_pct_series.tail(14).mean()) if not atr_pct_series.empty else 0.0
            realized_pct = float(returns_pct.tail(20).std(ddof=0)) if len(returns_pct) > 1 else 0.0
            volatility_pct = max(atr_pct, realized_pct)
            if not np.isfinite(volatility_pct):
                return
            result.volatility_pct = round(volatility_pct, 4)
            if volatility_pct <= self.VOLATILITY_LOW_PCT:
                state = VolatilityState.LOW
            elif volatility_pct <= self.VOLATILITY_HIGH_PCT:
                state = VolatilityState.NORMAL
            elif volatility_pct <= self.VOLATILITY_EXTREME_PCT:
                state = VolatilityState.HIGH
            else:
                state = VolatilityState.EXTREME
            result.volatility_state = state.value
        except Exception as exc:
            # Technical analysis is best effort; retain the neutral state when
            # malformed OHLC data prevents a volatility estimate.
            logger.debug("%s volatility estimate unavailable: %s", result.code, exc)

    def calibrate_thresholds(
        self,
        market_phase: Any = "unknown",
        volatility_state: Any = VolatilityState.NORMAL.value,
        direction: Any = SignalDirection.NEUTRAL.value,
        market_regime: str = "unknown",
        base_bias_threshold: Any = None,
        market_stage: Any = None,
        volatility: Any = None,
    ) -> Dict[str, Any]:
        """Return independent, state-aware signal thresholds.

        The profile intentionally exposes each multiplier. Callers can audit
        whether a stricter threshold came from the session phase, volatility,
        regime, or directional side instead of attributing it to a larger
        pile of indicators.
        """
        phase = self._normalize_market_phase(market_phase)
        volatility_state = volatility if volatility is not None else volatility_state
        if isinstance(volatility_state, Enum):
            volatility_state = volatility_state.value
        volatility = str(volatility_state or VolatilityState.NORMAL.value).strip().lower().replace("-", "_")
        volatility = {
            "compressed": VolatilityState.LOW.value,
            "elevated": VolatilityState.HIGH.value,
            "very_high": VolatilityState.EXTREME.value,
        }.get(volatility, volatility)
        if volatility not in self.VOLATILITY_THRESHOLD_MULTIPLIERS:
            volatility = VolatilityState.NORMAL.value
        if isinstance(direction, Enum):
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
        if regime not in {"bull_trend", "bear_trend", "range", "transition", "unknown"}:
            regime = "unknown"

        try:
            base_bias = float(base_bias_threshold) if base_bias_threshold is not None else float(get_config().bias_threshold)
        except (TypeError, ValueError, AttributeError):
            base_bias = 5.0
        if not np.isfinite(base_bias) or base_bias <= 0:
            base_bias = 5.0

        phase_multiplier = self.PHASE_THRESHOLD_MULTIPLIERS[phase]
        volatility_multiplier = self.VOLATILITY_THRESHOLD_MULTIPLIERS[volatility]
        volatility_bias_multiplier = self.VOLATILITY_BIAS_MULTIPLIERS[volatility]
        # Bias/chase distance is side-aware but not regime-aware: trend regime
        # is already represented in the score gate, while distance is a price
        # risk measure and should not be double-counted.
        direction_multiplier = {
            SignalDirection.LONG.value: 1.00,
            SignalDirection.SHORT.value: 1.05,
            SignalDirection.NEUTRAL.value: 1.10,
        }[direction]
        bias_multiplier = float(np.clip(phase_multiplier * volatility_bias_multiplier * direction_multiplier, 0.55, 1.50))
        bias_threshold = float(np.clip(base_bias * bias_multiplier, 2.0, 12.0))

        # Regime/side only calibrate score gates. The defaults (60/75 for long
        # and 60/70 for short) preserve the historical stock behavior.
        regime_long = {"bull_trend": 1.00, "bear_trend": 1.10, "range": 1.05, "transition": 1.05, "unknown": 1.00}[regime]
        regime_short = {"bull_trend": 1.10, "bear_trend": 1.00, "range": 1.05, "transition": 1.05, "unknown": 1.00}[regime]
        side_long = {"long": 1.00, "short": 1.10, "neutral": 1.05}[direction]
        side_short = {"long": 1.10, "short": 1.00, "neutral": 1.05}[direction]
        long_factor = phase_multiplier * volatility_multiplier * regime_long * side_long
        short_factor = phase_multiplier * volatility_multiplier * regime_short * side_short
        return {
            "market_phase": phase,
            "market_regime": regime,
            "market_stage": {
                "bull_trend": "bull",
                "bear_trend": "bear",
                "range": "range",
                "transition": "transition",
                "unknown": "unknown",
            }[regime],
            "volatility_state": volatility,
            "direction": direction,
            "phase_multiplier": round(float(phase_multiplier), 4),
            "volatility_multiplier": round(float(volatility_multiplier), 4),
            "volatility_bias_multiplier": round(float(volatility_bias_multiplier), 4),
            "direction_multiplier": round(float(direction_multiplier), 4),
            "regime_long_multiplier": round(float(regime_long), 4),
            "regime_short_multiplier": round(float(regime_short), 4),
            "side_long_multiplier": round(float(side_long), 4),
            "side_short_multiplier": round(float(side_short), 4),
            "bias_multiplier": round(bias_multiplier, 4),
            "bias_threshold": round(bias_threshold, 4),
            "bias_chase_threshold": round(float(np.clip(bias_threshold * 1.5, 3.0, 18.0)), 4),
            "long_entry_score": round(float(np.clip(60.0 * long_factor, 50.0, 88.0)), 2),
            "long_strong_score": round(float(np.clip(75.0 * long_factor, 65.0, 95.0)), 2),
            "short_entry_score": round(float(np.clip(60.0 * short_factor, 50.0, 88.0)), 2),
            "short_strong_score": round(float(np.clip(70.0 * short_factor, 60.0, 95.0)), 2),
        }

    def _calculate_mas(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算均线"""
        df = df.copy()
        df['MA5'] = df['close'].rolling(window=5).mean()
        df['MA10'] = df['close'].rolling(window=10).mean()
        df['MA20'] = df['close'].rolling(window=20).mean()
        if len(df) >= 60:
            df['MA60'] = df['close'].rolling(window=60).mean()
        else:
            df['MA60'] = df['MA20']  # 数据不足时使用 MA20 替代
        return df

    def _calculate_macd(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        计算 MACD 指标

        公式：
        - EMA(12)：12日指数移动平均
        - EMA(26)：26日指数移动平均
        - DIF = EMA(12) - EMA(26)
        - DEA = EMA(DIF, 9)
        - MACD = (DIF - DEA) * 2
        """
        df = df.copy()

        # 计算快慢线 EMA
        ema_fast = df['close'].ewm(span=self.MACD_FAST, adjust=False).mean()
        ema_slow = df['close'].ewm(span=self.MACD_SLOW, adjust=False).mean()

        # 计算快线 DIF
        df['MACD_DIF'] = ema_fast - ema_slow

        # 计算信号线 DEA
        df['MACD_DEA'] = df['MACD_DIF'].ewm(span=self.MACD_SIGNAL, adjust=False).mean()

        # 计算柱状图
        df['MACD_BAR'] = (df['MACD_DIF'] - df['MACD_DEA']) * 2

        return df

    def _calculate_rsi(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        计算 RSI 指标（Wilder's EMA / SMMA 口径）

        公式：
        - avg_gain / avg_loss 使用 ewm(alpha=1/period, adjust=False)
        - RS = avg_gain / avg_loss
        - RSI = 100 - (100 / (1 + RS))
        """
        df = df.copy()

        for period in [self.RSI_SHORT, self.RSI_MID, self.RSI_LONG]:
            # 计算价格变化
            delta = df['close'].diff()

            # 分离上涨和下跌
            gain = delta.where(delta > 0, 0)
            loss = -delta.where(delta < 0, 0)

            # 使用 Wilder's EMA / SMMA 口径，与常见 RSI 图表工具保持一致。
            avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
            avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

            # 计算 RS 和 RSI
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))

            # 填充 NaN 值
            rsi = rsi.fillna(50)  # 默认中性值

            # 添加到 DataFrame
            col_name = f'RSI_{period}'
            df[col_name] = rsi

        return df
    
    def _analyze_trend(self, df: pd.DataFrame, result: TrendAnalysisResult) -> None:
        """
        分析趋势状态
        
        核心逻辑：结合价格结构和均线排列判断趋势强度。

        均线是滞后指标，仅靠 MA5/10/20 的严格大小关系会把横盘中的微小
        差异误判成弱趋势。因此先计算价格斜率、区间振幅和方向效率，再用
        容差判断均线排列；价格结构明确时，允许在均线尚未完全排列前给出
        “弱势多/空头”，而不是直接返回趋势不明。
        """
        ma5, ma10, ma20 = result.ma5, result.ma10, result.ma20

        price_trend = self._analyze_price_structure(df, result)
        ma_tolerance = abs(ma20) * self.MA_ALIGNMENT_TOLERANCE_PCT if ma20 > 0 else 0.0

        ma5_above_ma10 = ma5 > ma10 + ma_tolerance
        ma10_above_ma20 = ma10 > ma20 + ma_tolerance
        ma5_below_ma10 = ma5 < ma10 - ma_tolerance
        ma10_below_ma20 = ma10 < ma20 - ma_tolerance

        # 区间窄、路径来回反复时，盘整优先级高于均线的微弱排列。
        is_consolidating = (
            price_trend == "震荡"
            and (
                result.price_range_pct <= self.PRICE_CONSOLIDATION_RANGE_PCT
                or result.directional_efficiency <= self.PRICE_CONSOLIDATION_EFFICIENCY
            )
        )

        if is_consolidating:
            result.trend_status = TrendStatus.CONSOLIDATION
            result.ma_alignment = "价格区间震荡，均线缠绕"
            result.trend_strength = 50
            return
        
        # 判断均线排列
        if ma5_above_ma10 and ma10_above_ma20 and price_trend != "下跌":
            # 检查间距是否在扩大（强势）
            prev = df.iloc[-5] if len(df) >= 5 else df.iloc[-1]
            prev_spread = (prev['MA5'] - prev['MA20']) / prev['MA20'] * 100 if prev['MA20'] > 0 else 0
            curr_spread = (ma5 - ma20) / ma20 * 100 if ma20 > 0 else 0
            
            if curr_spread > prev_spread and curr_spread > 5:
                result.trend_status = TrendStatus.STRONG_BULL
                result.ma_alignment = "强势多头排列，均线发散上行"
                result.trend_strength = 90
            else:
                result.trend_status = TrendStatus.BULL
                result.ma_alignment = "多头排列 MA5>MA10>MA20"
                result.trend_strength = 75
                
        elif (
            (ma5_above_ma10 and not ma10_above_ma20)
            or price_trend == "上涨"
        ):
            result.trend_status = TrendStatus.WEAK_BULL
            result.ma_alignment = "弱势多头，MA5>MA10 但 MA10≤MA20"
            result.trend_strength = 55
            
        elif ma5_below_ma10 and ma10_below_ma20 and price_trend != "上涨":
            prev = df.iloc[-5] if len(df) >= 5 else df.iloc[-1]
            prev_spread = (prev['MA20'] - prev['MA5']) / prev['MA5'] * 100 if prev['MA5'] > 0 else 0
            curr_spread = (ma20 - ma5) / ma5 * 100 if ma5 > 0 else 0
            
            if curr_spread > prev_spread and curr_spread > 5:
                result.trend_status = TrendStatus.STRONG_BEAR
                result.ma_alignment = "强势空头排列，均线发散下行"
                result.trend_strength = 10
            else:
                result.trend_status = TrendStatus.BEAR
                result.ma_alignment = "空头排列 MA5<MA10<MA20"
                result.trend_strength = 25
                
        elif (
            (ma5_below_ma10 and not ma10_below_ma20)
            or price_trend == "下跌"
        ):
            result.trend_status = TrendStatus.WEAK_BEAR
            result.ma_alignment = "弱势空头，MA5<MA10 但 MA10≥MA20"
            result.trend_strength = 40
            
        else:
            result.trend_status = TrendStatus.CONSOLIDATION
            result.ma_alignment = "均线缠绕，趋势不明"
            result.trend_strength = 50

    def _analyze_price_structure(self, df: pd.DataFrame, result: TrendAnalysisResult) -> str:
        """从收盘价的方向、振幅和路径效率识别上涨/下跌/震荡。"""
        if "close" not in df.columns:
            return result.price_trend

        close = pd.to_numeric(df["close"], errors="coerce").dropna()
        close = close.tail(min(self.PRICE_TREND_LOOKBACK, len(close)))
        if len(close) < 5:
            return result.price_trend

        result.price_structure_available = True

        values = close.to_numpy(dtype=float)
        first_price = float(values[0])
        mean_price = float(np.mean(values))
        if not np.isfinite(first_price) or not np.isfinite(mean_price) or first_price <= 0 or mean_price <= 0:
            return result.price_trend

        x = np.arange(len(values), dtype=float)
        slope = float(np.polyfit(x, values, 1)[0])
        slope_pct = slope * (len(values) - 1) / mean_price * 100
        return_pct = (float(values[-1]) - first_price) / first_price * 100
        range_pct = (float(np.max(values)) - float(np.min(values))) / first_price * 100
        path = float(np.abs(np.diff(values)).sum())
        efficiency = abs(float(values[-1]) - first_price) / path if path > 0 else 0.0
        efficiency = float(np.clip(efficiency, 0.0, 1.0))

        result.price_slope_pct = round(slope_pct, 4)
        result.price_return_pct = round(return_pct, 4)
        result.price_range_pct = round(range_pct, 4)
        result.directional_efficiency = round(efficiency, 4)

        threshold = self.PRICE_DIRECTION_THRESHOLD_PCT
        directional_up = (
            slope_pct >= threshold and efficiency >= self.PRICE_CONSOLIDATION_EFFICIENCY
        ) or (return_pct >= threshold * 1.5 and efficiency >= 0.35)
        directional_down = (
            slope_pct <= -threshold and efficiency >= self.PRICE_CONSOLIDATION_EFFICIENCY
        ) or (return_pct <= -threshold * 1.5 and efficiency >= 0.35)

        if directional_up and not directional_down:
            result.price_trend = "上涨"
        elif directional_down and not directional_up:
            result.price_trend = "下跌"
        else:
            result.price_trend = "震荡"
        return result.price_trend
    
    def _calculate_bias(self, result: TrendAnalysisResult) -> None:
        """
        计算乖离率
        
        乖离率 = (现价 - 均线) / 均线 * 100%
        
        严进策略：乖离率超过 5% 不追高
        """
        price = result.current_price
        
        if result.ma5 > 0:
            result.bias_ma5 = (price - result.ma5) / result.ma5 * 100
        if result.ma10 > 0:
            result.bias_ma10 = (price - result.ma10) / result.ma10 * 100
        if result.ma20 > 0:
            result.bias_ma20 = (price - result.ma20) / result.ma20 * 100
    
    def _analyze_volume(self, df: pd.DataFrame, result: TrendAnalysisResult) -> None:
        """
        分析量能
        
        偏好：缩量回调 > 放量上涨 > 缩量上涨 > 放量下跌
        """
        if len(df) < 5:
            return
        
        latest = df.iloc[-1]
        vol_5d_avg = df['volume'].iloc[-6:-1].mean()
        
        if vol_5d_avg > 0:
            result.volume_ratio_5d = float(latest['volume']) / vol_5d_avg
        
        # 判断价格变化
        prev_close = df.iloc[-2]['close']
        price_change = (latest['close'] - prev_close) / prev_close * 100
        
        # 量能状态判断
        if result.volume_ratio_5d >= self.VOLUME_HEAVY_RATIO:
            if price_change > 0:
                result.volume_status = VolumeStatus.HEAVY_VOLUME_UP
                result.volume_trend = "放量上涨，多头力量强劲"
            else:
                result.volume_status = VolumeStatus.HEAVY_VOLUME_DOWN
                result.volume_trend = "放量下跌，注意风险"
        elif result.volume_ratio_5d <= self.VOLUME_SHRINK_RATIO:
            if price_change > 0:
                result.volume_status = VolumeStatus.SHRINK_VOLUME_UP
                result.volume_trend = "缩量上涨，上攻动能不足"
            else:
                result.volume_status = VolumeStatus.SHRINK_VOLUME_DOWN
                result.volume_trend = "缩量回调，洗盘特征明显（好）"
        else:
            result.volume_status = VolumeStatus.NORMAL
            result.volume_trend = "量能正常"
    
    def _analyze_support_resistance(self, df: pd.DataFrame, result: TrendAnalysisResult) -> None:
        """
        分析支撑压力位
        
        买点偏好：回踩 MA5/MA10 获得支撑
        """
        price = result.current_price
        
        # 检查是否在 MA5 附近获得支撑
        if result.ma5 > 0:
            ma5_distance = abs(price - result.ma5) / result.ma5
            if ma5_distance <= self.MA_SUPPORT_TOLERANCE and price >= result.ma5:
                result.support_ma5 = True
                result.support_levels.append(result.ma5)
        
        # 检查是否在 MA10 附近获得支撑
        if result.ma10 > 0:
            ma10_distance = abs(price - result.ma10) / result.ma10
            if ma10_distance <= self.MA_SUPPORT_TOLERANCE and price >= result.ma10:
                result.support_ma10 = True
                if result.ma10 not in result.support_levels:
                    result.support_levels.append(result.ma10)
        
        # MA20 作为重要支撑
        if result.ma20 > 0 and price >= result.ma20:
            result.support_levels.append(result.ma20)
        
        # 近期高点作为压力
        if len(df) >= 20:
            recent_high = df['high'].iloc[-20:].max()
            if recent_high > price:
                result.resistance_levels.append(recent_high)

    def _analyze_macd(self, df: pd.DataFrame, result: TrendAnalysisResult) -> None:
        """
        分析 MACD 指标

        核心信号：
        - 零轴上金叉：最强买入信号
        - 金叉：DIF 上穿 DEA
        - 死叉：DIF 下穿 DEA
        """
        if len(df) < self.MACD_SLOW:
            result.macd_signal = "数据不足"
            return

        latest = df.iloc[-1]
        prev = df.iloc[-2]

        # 获取 MACD 数据
        result.macd_dif = float(latest['MACD_DIF'])
        result.macd_dea = float(latest['MACD_DEA'])
        result.macd_bar = float(latest['MACD_BAR'])

        # 判断金叉死叉
        prev_dif_dea = prev['MACD_DIF'] - prev['MACD_DEA']
        curr_dif_dea = result.macd_dif - result.macd_dea

        # 金叉：DIF 上穿 DEA
        is_golden_cross = prev_dif_dea <= 0 and curr_dif_dea > 0

        # 死叉：DIF 下穿 DEA
        is_death_cross = prev_dif_dea >= 0 and curr_dif_dea < 0

        # 零轴穿越
        prev_zero = prev['MACD_DIF']
        curr_zero = result.macd_dif
        is_crossing_up = prev_zero <= 0 and curr_zero > 0
        is_crossing_down = prev_zero >= 0 and curr_zero < 0

        # 判断 MACD 状态
        if is_golden_cross and curr_zero > 0:
            result.macd_status = MACDStatus.GOLDEN_CROSS_ZERO
            result.macd_signal = "⭐ 零轴上金叉，强烈买入信号！"
        elif is_crossing_up:
            result.macd_status = MACDStatus.CROSSING_UP
            result.macd_signal = "⚡ DIF上穿零轴，趋势转强"
        elif is_golden_cross:
            result.macd_status = MACDStatus.GOLDEN_CROSS
            result.macd_signal = "✅ 金叉，趋势向上"
        elif is_death_cross:
            result.macd_status = MACDStatus.DEATH_CROSS
            result.macd_signal = "❌ 死叉，趋势向下"
        elif is_crossing_down:
            result.macd_status = MACDStatus.CROSSING_DOWN
            result.macd_signal = "⚠️ DIF下穿零轴，趋势转弱"
        elif result.macd_dif > 0 and result.macd_dea > 0:
            result.macd_status = MACDStatus.BULLISH
            result.macd_signal = "✓ 多头排列，持续上涨"
        elif result.macd_dif < 0 and result.macd_dea < 0:
            result.macd_status = MACDStatus.BEARISH
            result.macd_signal = "⚠ 空头排列，持续下跌"
        else:
            result.macd_status = MACDStatus.BULLISH
            result.macd_signal = " MACD 中性区域"

    def _analyze_rsi(self, df: pd.DataFrame, result: TrendAnalysisResult) -> None:
        """
        分析 RSI 指标

        核心判断：
        - RSI > 70：超买，谨慎追高
        - RSI < 30：超卖，关注反弹
        - 40-60：中性区域
        """
        if len(df) < self.RSI_LONG:
            result.rsi_signal = "数据不足"
            return

        latest = df.iloc[-1]

        # 获取 RSI 数据
        result.rsi_6 = float(latest[f'RSI_{self.RSI_SHORT}'])
        result.rsi_12 = float(latest[f'RSI_{self.RSI_MID}'])
        result.rsi_24 = float(latest[f'RSI_{self.RSI_LONG}'])

        # 以中期 RSI(12) 为主进行判断
        rsi_mid = result.rsi_12

        # 判断 RSI 状态
        if rsi_mid > self.RSI_OVERBOUGHT:
            result.rsi_status = RSIStatus.OVERBOUGHT
            result.rsi_signal = f"⚠️ RSI超买({rsi_mid:.1f}>70)，短期回调风险高"
        elif rsi_mid > 60:
            result.rsi_status = RSIStatus.STRONG_BUY
            result.rsi_signal = f"✅ RSI强势({rsi_mid:.1f})，多头力量充足"
        elif rsi_mid >= 40:
            result.rsi_status = RSIStatus.NEUTRAL
            result.rsi_signal = f" RSI中性({rsi_mid:.1f})，震荡整理中"
        elif rsi_mid >= self.RSI_OVERSOLD:
            result.rsi_status = RSIStatus.WEAK
            result.rsi_signal = f"⚡ RSI弱势({rsi_mid:.1f})，关注反弹"
        else:
            result.rsi_status = RSIStatus.OVERSOLD
            result.rsi_signal = f"⭐ RSI超卖({rsi_mid:.1f}<30)，反弹机会大"

    def _generate_signal(self, result: TrendAnalysisResult) -> None:
        """
        生成买入信号

        综合评分系统：
        - 趋势（30分）：多头排列得分高
        - 乖离率（20分）：接近 MA5 得分高
        - 量能（15分）：缩量回调得分高
        - 支撑（10分）：获得均线支撑得分高
        - MACD（15分）：金叉和多头得分高
        - RSI（10分）：超卖和强势得分高
        """
        score = 0
        reasons = []
        risks = []

        # === 趋势评分（30分）===
        trend_scores = {
            TrendStatus.STRONG_BULL: 30,
            TrendStatus.BULL: 26,
            TrendStatus.WEAK_BULL: 18,
            TrendStatus.CONSOLIDATION: 12,
            TrendStatus.WEAK_BEAR: 8,
            TrendStatus.BEAR: 4,
            TrendStatus.STRONG_BEAR: 0,
        }
        trend_score = trend_scores.get(result.trend_status, 12)
        score += trend_score

        if result.trend_status in [TrendStatus.STRONG_BULL, TrendStatus.BULL]:
            reasons.append(f"✅ {result.trend_status.value}，顺势做多")
        elif result.trend_status in [TrendStatus.BEAR, TrendStatus.STRONG_BEAR]:
            risks.append(f"⚠️ {result.trend_status.value}，不宜做多")

        # === 乖离率评分（20分，强势趋势补偿）===
        bias = result.bias_ma5
        if bias != bias or bias is None:  # NaN or None defense
            bias = 0.0
        profile = self.calibrate_thresholds(
            market_phase=result.market_phase,
            volatility_state=result.volatility_state,
            direction=self._infer_signal_direction(result),
            market_regime=(
                result.market_regime
                if result.market_regime and result.market_regime != "unknown"
                else self._infer_market_regime(result)
            ),
        )
        result.threshold_profile = profile
        result.signal_direction = str(profile["direction"])
        base_threshold = float(profile["bias_threshold"])

        # Strong trend compensation: relax threshold for STRONG_BULL with high strength
        trend_strength = result.trend_strength if result.trend_strength == result.trend_strength else 0.0
        if result.trend_status == TrendStatus.STRONG_BULL and (trend_strength or 0) >= 70:
            effective_threshold = float(profile["bias_chase_threshold"])
            is_strong_trend = True
        else:
            effective_threshold = base_threshold
            is_strong_trend = False

        if profile["direction"] == SignalDirection.SHORT.value:
            # In a bearish structure, a price already below MA5 is extended
            # to the downside; do not turn that move into a long "pullback"
            # point. A rebound toward MA5 is the only bias pattern that can
            # improve a short-side setup.
            if bias >= 0:
                if bias < 2:
                    score += 18
                    reasons.append(f"✅ 价格反弹贴近MA5({bias:.1f}%)，空头观察点")
                elif bias < base_threshold:
                    score += 14
                    reasons.append(f"⚡ 价格反弹至MA5上方({bias:.1f}%)，可观察空头确认")
                elif bias > effective_threshold:
                    score += 4
                    risks.append(f"❌ 反弹乖离过高({bias:.1f}%>{effective_threshold:.1f}%)，等待回落确认")
                else:
                    score += 8
                    reasons.append(f"⚡ 空头方向乖离偏高({bias:.1f}%)，等待反转确认")
            elif bias > -3:
                score += 8
                risks.append(f"⚠️ 价格低于MA5({bias:.1f}%)，空头追跌风险")
            elif bias > -5:
                score += 4
                risks.append(f"⚠️ 下行乖离扩大({bias:.1f}%)，不宜追空")
            else:
                risks.append(f"❌ 下行乖离过大({bias:.1f}%)，等待反弹后再评估")
        elif bias < 0:
            # Price below MA5 (pullback)
            if bias > -3:
                score += 20
                reasons.append(f"✅ 价格略低于MA5({bias:.1f}%)，回踩买点")
            elif bias > -5:
                score += 16
                reasons.append(f"✅ 价格回踩MA5({bias:.1f}%)，观察支撑")
            else:
                score += 8
                risks.append(f"⚠️ 乖离率过大({bias:.1f}%)，可能破位")
        elif bias < 2:
            score += 18
            reasons.append(f"✅ 价格贴近MA5({bias:.1f}%)，介入好时机")
        elif bias < base_threshold:
            score += 14
            reasons.append(f"⚡ 价格略高于MA5({bias:.1f}%)，可小仓介入")
        elif bias > effective_threshold:
            score += 4
            risks.append(
                f"❌ 乖离率过高({bias:.1f}%>{effective_threshold:.1f}%)，严禁追高！"
            )
        elif bias > base_threshold and is_strong_trend:
            score += 10
            reasons.append(
                f"⚡ 强势趋势中乖离率偏高({bias:.1f}%)，可轻仓追踪"
            )
        else:
            score += 4
            risks.append(
                f"❌ 乖离率过高({bias:.1f}%>{base_threshold:.1f}%)，严禁追高！"
            )

        # === 量能评分（15分）===
        volume_scores = {
            VolumeStatus.SHRINK_VOLUME_DOWN: 15,  # 缩量回调最佳
            VolumeStatus.HEAVY_VOLUME_UP: 12,     # 放量上涨次之
            VolumeStatus.NORMAL: 10,
            VolumeStatus.SHRINK_VOLUME_UP: 6,     # 无量上涨较差
            VolumeStatus.HEAVY_VOLUME_DOWN: 0,    # 放量下跌最差
        }
        vol_score = volume_scores.get(result.volume_status, 8)
        score += vol_score

        if result.volume_status == VolumeStatus.SHRINK_VOLUME_DOWN:
            reasons.append("✅ 缩量回调，主力洗盘")
        elif result.volume_status == VolumeStatus.HEAVY_VOLUME_DOWN:
            risks.append("⚠️ 放量下跌，注意风险")

        # === 支撑评分（10分）===
        if result.support_ma5:
            score += 5
            reasons.append("✅ MA5支撑有效")
        if result.support_ma10:
            score += 5
            reasons.append("✅ MA10支撑有效")

        # === MACD 评分（15分）===
        macd_scores = {
            MACDStatus.GOLDEN_CROSS_ZERO: 15,  # 零轴上金叉最强
            MACDStatus.GOLDEN_CROSS: 12,      # 金叉
            MACDStatus.CROSSING_UP: 10,       # 上穿零轴
            MACDStatus.BULLISH: 8,            # 多头
            MACDStatus.BEARISH: 2,            # 空头
            MACDStatus.CROSSING_DOWN: 0,       # 下穿零轴
            MACDStatus.DEATH_CROSS: 0,        # 死叉
        }
        macd_score = macd_scores.get(result.macd_status, 5)
        score += macd_score

        if result.macd_status in [MACDStatus.GOLDEN_CROSS_ZERO, MACDStatus.GOLDEN_CROSS]:
            reasons.append(f"✅ {result.macd_signal}")
        elif result.macd_status in [MACDStatus.DEATH_CROSS, MACDStatus.CROSSING_DOWN]:
            risks.append(f"⚠️ {result.macd_signal}")
        else:
            reasons.append(result.macd_signal)

        # === RSI 评分（10分）===
        rsi_scores = {
            RSIStatus.OVERSOLD: 10,       # 超卖最佳
            RSIStatus.STRONG_BUY: 8,     # 强势
            RSIStatus.NEUTRAL: 5,        # 中性
            RSIStatus.WEAK: 3,            # 弱势
            RSIStatus.OVERBOUGHT: 0,       # 超买最差
        }
        rsi_score = rsi_scores.get(result.rsi_status, 5)
        score += rsi_score

        if result.rsi_status in [RSIStatus.OVERSOLD, RSIStatus.STRONG_BUY]:
            reasons.append(f"✅ {result.rsi_signal}")
        elif result.rsi_status == RSIStatus.OVERBOUGHT:
            risks.append(f"⚠️ {result.rsi_signal}")
        else:
            reasons.append(result.rsi_signal)

        # === 综合判断 ===
        result.signal_score = score
        result.signal_reasons = reasons
        result.risk_factors = risks

        # Generate the action from calibrated side-specific gates. The score
        # remains a transparent summary, while the inverse score gives the
        # short side a symmetric decision boundary without adding indicators.
        short_score = 100 - score
        if score >= profile["long_strong_score"] and result.trend_status in [TrendStatus.STRONG_BULL, TrendStatus.BULL]:
            result.buy_signal = BuySignal.STRONG_BUY
        elif score >= profile["long_entry_score"] and result.trend_status in [TrendStatus.STRONG_BULL, TrendStatus.BULL, TrendStatus.WEAK_BULL]:
            result.buy_signal = BuySignal.BUY
        elif short_score >= profile["short_strong_score"] and result.trend_status in [TrendStatus.STRONG_BEAR, TrendStatus.BEAR]:
            result.buy_signal = BuySignal.STRONG_SELL
        elif short_score >= profile["short_entry_score"] and result.trend_status in [TrendStatus.STRONG_BEAR, TrendStatus.BEAR, TrendStatus.WEAK_BEAR]:
            result.buy_signal = BuySignal.SELL
        elif score >= 45:
            result.buy_signal = BuySignal.HOLD
        elif score >= 30:
            result.buy_signal = BuySignal.WAIT
        elif result.trend_status in [TrendStatus.BEAR, TrendStatus.STRONG_BEAR]:
            result.buy_signal = BuySignal.STRONG_SELL
        else:
            result.buy_signal = BuySignal.SELL

    def format_analysis(self, result: TrendAnalysisResult) -> str:
        """
        格式化分析结果为文本

        Args:
            result: 分析结果

        Returns:
            格式化的分析文本
        """
        lines = [
            f"=== {result.code} 趋势分析 ===",
            f"",
            f"📊 趋势判断: {result.trend_status.value}",
            f"   均线排列: {result.ma_alignment}",
            f"   趋势强度: {result.trend_strength}/100",
            f"   市场阶段: {result.market_phase} | 趋势阶段: {result.market_regime}",
            f"   波动状态: {result.volatility_state} ({result.volatility_pct:.2f}%) | 校准方向: {result.signal_direction}",
            f"",
            f"📈 均线数据:",
            f"   现价: {result.current_price:.2f}",
            f"   MA5:  {result.ma5:.2f} (乖离 {result.bias_ma5:+.2f}%)",
            f"   MA10: {result.ma10:.2f} (乖离 {result.bias_ma10:+.2f}%)",
            f"   MA20: {result.ma20:.2f} (乖离 {result.bias_ma20:+.2f}%)",
            f"",
            f"📊 量能分析: {result.volume_status.value}",
            f"   量比(vs5日): {result.volume_ratio_5d:.2f}",
            f"   量能趋势: {result.volume_trend}",
            f"",
            f"📈 MACD指标: {result.macd_status.value}",
            f"   DIF: {result.macd_dif:.4f}",
            f"   DEA: {result.macd_dea:.4f}",
            f"   MACD: {result.macd_bar:.4f}",
            f"   信号: {result.macd_signal}",
            f"",
            f"📊 RSI指标: {result.rsi_status.value}",
            f"   RSI(6): {result.rsi_6:.1f}",
            f"   RSI(12): {result.rsi_12:.1f}",
            f"   RSI(24): {result.rsi_24:.1f}",
            f"   信号: {result.rsi_signal}",
            f"",
            f"🎯 操作建议: {result.buy_signal.value}",
            f"   综合评分: {result.signal_score}/100",
        ]

        profile = result.threshold_profile or {}
        if profile:
            lines.append(
                "   校准门槛: "
                f"乖离≤{profile.get('bias_threshold', 0):.2f}% | "
                f"多头{profile.get('long_entry_score', 0):.1f}/{profile.get('long_strong_score', 0):.1f} | "
                f"空头{profile.get('short_entry_score', 0):.1f}/{profile.get('short_strong_score', 0):.1f}"
            )

        if result.signal_reasons:
            lines.append(f"")
            lines.append(f"✅ 买入理由:")
            for reason in result.signal_reasons:
                lines.append(f"   {reason}")

        if result.risk_factors:
            lines.append(f"")
            lines.append(f"⚠️ 风险因素:")
            for risk in result.risk_factors:
                lines.append(f"   {risk}")

        return "\n".join(lines)


def analyze_stock(
    df: pd.DataFrame,
    code: str,
    market_phase_context: Any = None,
    market_phase: Any = None,
) -> TrendAnalysisResult:
    """
    便捷函数：分析单只股票
    
    Args:
        df: 包含 OHLCV 数据的 DataFrame
        code: 股票代码
        
    Returns:
        TrendAnalysisResult 分析结果
    """
    analyzer = StockTrendAnalyzer()
    return analyzer.analyze(
        df,
        code,
        market_phase_context=market_phase_context,
        market_phase=market_phase,
    )


if __name__ == "__main__":
    # 测试代码
    logging.basicConfig(level=logging.INFO)
    
    # 模拟数据测试
    import numpy as np
    
    dates = pd.date_range(start='2025-01-01', periods=60, freq='D')
    np.random.seed(42)
    
    # 模拟多头排列的数据
    base_price = 10.0
    prices = [base_price]
    for i in range(59):
        change = np.random.randn() * 0.02 + 0.003  # 轻微上涨趋势
        prices.append(prices[-1] * (1 + change))
    
    df = pd.DataFrame({
        'date': dates,
        'open': prices,
        'high': [p * (1 + np.random.uniform(0, 0.02)) for p in prices],
        'low': [p * (1 - np.random.uniform(0, 0.02)) for p in prices],
        'close': prices,
        'volume': [np.random.randint(1000000, 5000000) for _ in prices],
    })
    
    analyzer = StockTrendAnalyzer()
    result = analyzer.analyze(df, '000001')
    print(analyzer.format_analysis(result))
