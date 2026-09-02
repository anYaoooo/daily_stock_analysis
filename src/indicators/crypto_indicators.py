# -*- coding: utf-8 -*-
"""Crypto-specific technical indicators for BTC analysis.

This module provides cryptocurrency-specific indicators that are not typically
used in traditional stock analysis, including funding rates, open interest,
long/short ratios, and on-chain metrics.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class FundingRateAnalysis:
    """Funding rate analysis for perpetual futures."""

    current_rate: float
    avg_rate_24h: float
    avg_rate_7d: float
    trend: str  # "positive", "negative", "neutral"
    extremity: str  # "extreme_long", "extreme_short", "normal"
    interpretation: str

    @classmethod
    def analyze(cls, funding_rates: List[float]) -> FundingRateAnalysis:
        """Analyze funding rate data.

        Args:
            funding_rates: List of recent funding rates (most recent first)

        Returns:
            FundingRateAnalysis with interpretation
        """
        if not funding_rates:
            return cls(
                current_rate=0.0,
                avg_rate_24h=0.0,
                avg_rate_7d=0.0,
                trend="neutral",
                extremity="normal",
                interpretation="No funding rate data available"
            )

        current = funding_rates[0]
        # Binance/OKX funding is normally settled every 8 hours: 3 samples per
        # day and 21 samples per week. The previous 72/504 windows silently
        # collapsed almost every real response to ``current``.
        avg_24h = statistics.mean(funding_rates[:3])
        avg_7d = statistics.mean(funding_rates[:21])

        # Determine trend
        delta = current - avg_24h
        trend_threshold = max(abs(avg_24h) * 0.5, 0.00001)
        if delta > trend_threshold:
            trend = "positive"
        elif delta < -trend_threshold:
            trend = "negative"
        else:
            trend = "neutral"

        # Funding values are fractions (0.0001 = 0.01% per settlement).
        if current > 0.0005:
            extremity = "extreme_long"
            interpretation = "极高正费率，多头过度拥挤，可能面临回调压力"
        elif current < -0.0005:
            extremity = "extreme_short"
            interpretation = "极高负费率，空头过度拥挤，可能面临空头挤压"
        elif current > 0.0001:
            extremity = "elevated_long"
            interpretation = "正费率偏高，多头成本增加，需警惕多头疲劳"
        elif current < -0.0001:
            extremity = "elevated_short"
            interpretation = "负费率偏高，空头成本增加，利于多头"
        else:
            extremity = "normal"
            interpretation = "费率正常，多空相对平衡"

        return cls(
            current_rate=current,
            avg_rate_24h=avg_24h,
            avg_rate_7d=avg_7d,
            trend=trend,
            extremity=extremity,
            interpretation=interpretation
        )


@dataclass
class OpenInterestAnalysis:
    """Open interest analysis for futures contracts."""

    current_oi: float
    oi_change_24h_pct: float
    oi_change_7d_pct: float
    price_oi_divergence: str  # "bullish", "bearish", "neutral"
    interpretation: str

    @classmethod
    def analyze(
        cls,
        current_oi: float,
        oi_24h_ago: float,
        oi_7d_ago: float,
        price_change_24h_pct: float
    ) -> OpenInterestAnalysis:
        """Analyze open interest changes.

        Args:
            current_oi: Current open interest value
            oi_24h_ago: Open interest 24 hours ago
            oi_7d_ago: Open interest 7 days ago
            price_change_24h_pct: Price change in last 24 hours (%)

        Returns:
            OpenInterestAnalysis with interpretation
        """
        oi_change_24h = ((current_oi - oi_24h_ago) / oi_24h_ago * 100) if oi_24h_ago > 0 else 0.0
        oi_change_7d = ((current_oi - oi_7d_ago) / oi_7d_ago * 100) if oi_7d_ago > 0 else 0.0

        # Analyze price-OI divergence
        if price_change_24h_pct > 2 and oi_change_24h > 5:
            divergence = "bullish"
            interpretation = "价格上涨且持仓量增加，多头趋势强劲"
        elif price_change_24h_pct < -2 and oi_change_24h > 5:
            divergence = "bearish"
            interpretation = "价格下跌但持仓量增加，空头趋势强劲"
        elif price_change_24h_pct > 2 and oi_change_24h < -5:
            divergence = "bullish"
            interpretation = "价格上涨但持仓量减少，主要可能是空头回补；方向偏多但上涨延续性低于增仓上涨"
        elif price_change_24h_pct < -2 and oi_change_24h < -5:
            divergence = "bearish"
            interpretation = "价格下跌且持仓量减少，主要可能是多头平仓；短线偏空但趋势确认度较低"
        else:
            divergence = "neutral"
            interpretation = "持仓量变化正常，无明显异常信号"

        return cls(
            current_oi=current_oi,
            oi_change_24h_pct=oi_change_24h,
            oi_change_7d_pct=oi_change_7d,
            price_oi_divergence=divergence,
            interpretation=interpretation
        )


@dataclass
class LongShortRatioAnalysis:
    """Long/short ratio analysis from exchange data."""

    long_short_ratio: float
    long_pct: float
    short_pct: float
    sentiment: str  # "extreme_greed", "greed", "neutral", "fear", "extreme_fear"
    interpretation: str

    @classmethod
    def analyze(cls, long_accounts: int, short_accounts: int) -> LongShortRatioAnalysis:
        """Analyze long/short ratio.

        Args:
            long_accounts: Number of accounts holding long positions
            short_accounts: Number of accounts holding short positions

        Returns:
            LongShortRatioAnalysis with interpretation
        """
        total = long_accounts + short_accounts
        if total == 0:
            return cls(
                long_short_ratio=1.0,
                long_pct=50.0,
                short_pct=50.0,
                sentiment="neutral",
                interpretation="无多空数据"
            )

        ratio = long_accounts / short_accounts if short_accounts > 0 else 10.0
        long_pct = (long_accounts / total) * 100
        short_pct = (short_accounts / total) * 100

        # Determine sentiment
        if ratio > 3.0:
            sentiment = "extreme_greed"
            interpretation = f"多头极度拥挤({long_pct:.1f}% 多头)，市场过度乐观，警惕回调"
        elif ratio > 2.0:
            sentiment = "greed"
            interpretation = f"多头占优({long_pct:.1f}% 多头)，市场偏向乐观"
        elif ratio < 0.33:
            sentiment = "extreme_fear"
            interpretation = f"空头极度拥挤({short_pct:.1f}% 空头)，市场过度悲观，可能反弹"
        elif ratio < 0.5:
            sentiment = "fear"
            interpretation = f"空头占优({short_pct:.1f}% 空头)，市场偏向悲观"
        else:
            sentiment = "neutral"
            interpretation = f"多空相对平衡({long_pct:.1f}% 多头 vs {short_pct:.1f}% 空头)"

        return cls(
            long_short_ratio=ratio,
            long_pct=long_pct,
            short_pct=short_pct,
            sentiment=sentiment,
            interpretation=interpretation
        )


@dataclass
class LiquidationAnalysis:
    """Liquidation heatmap and cluster analysis."""

    liquidation_clusters_above: List[float]  # Price levels with liquidation clusters above
    liquidation_clusters_below: List[float]  # Price levels with liquidation clusters below
    nearest_cluster_above: Optional[float]
    nearest_cluster_below: Optional[float]
    interpretation: str

    @classmethod
    def analyze(
        cls,
        current_price: float,
        liquidation_map: Dict[float, float]
    ) -> LiquidationAnalysis:
        """Analyze liquidation clusters.

        Args:
            current_price: Current BTC price
            liquidation_map: Dict mapping price levels to liquidation amounts

        Returns:
            LiquidationAnalysis with interpretation
        """
        if not liquidation_map:
            return cls(
                liquidation_clusters_above=[],
                liquidation_clusters_below=[],
                nearest_cluster_above=None,
                nearest_cluster_below=None,
                interpretation="无清算数据"
            )

        # Find significant clusters (>10% above median liquidation amount)
        liquidation_amounts = list(liquidation_map.values())
        median_amount = statistics.median(liquidation_amounts) if liquidation_amounts else 0
        threshold = median_amount * 1.5

        clusters_above = sorted([
            price for price, amount in liquidation_map.items()
            if price > current_price and amount > threshold
        ])

        clusters_below = sorted([
            price for price, amount in liquidation_map.items()
            if price < current_price and amount > threshold
        ], reverse=True)

        nearest_above = clusters_above[0] if clusters_above else None
        nearest_below = clusters_below[0] if clusters_below else None

        # Generate interpretation
        if nearest_above and nearest_below:
            above_dist = ((nearest_above - current_price) / current_price) * 100
            below_dist = ((current_price - nearest_below) / current_price) * 100
            interpretation = (
                f"上方 {above_dist:.2f}% 和下方 {below_dist:.2f}% 存在清算集群，"
                f"价格可能在这些区域遇到阻力或支撑"
            )
        elif nearest_above:
            above_dist = ((nearest_above - current_price) / current_price) * 100
            interpretation = f"上方 {above_dist:.2f}% 存在清算集群，上涨可能遇阻"
        elif nearest_below:
            below_dist = ((current_price - nearest_below) / current_price) * 100
            interpretation = f"下方 {below_dist:.2f}% 存在清算集群，下跌可能获得支撑"
        else:
            interpretation = "未发现显著清算集群"

        return cls(
            liquidation_clusters_above=clusters_above[:5],  # Top 5
            liquidation_clusters_below=clusters_below[:5],  # Top 5
            nearest_cluster_above=nearest_above,
            nearest_cluster_below=nearest_below,
            interpretation=interpretation
        )


@dataclass
class CryptoIndicatorsSummary:
    """Comprehensive summary of crypto-specific indicators."""

    funding_rate: Optional[FundingRateAnalysis] = None
    open_interest: Optional[OpenInterestAnalysis] = None
    long_short_ratio: Optional[LongShortRatioAnalysis] = None
    liquidation: Optional[LiquidationAnalysis] = None

    overall_sentiment: str = "neutral"
    key_insights: List[str] = None
    risk_warnings: List[str] = None

    def __post_init__(self):
        """Initialize lists if None."""
        if self.key_insights is None:
            self.key_insights = []
        if self.risk_warnings is None:
            self.risk_warnings = []

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        result = {
            "overall_sentiment": self.overall_sentiment,
            "key_insights": self.key_insights,
            "risk_warnings": self.risk_warnings,
        }

        if self.funding_rate:
            result["funding_rate"] = {
                "current": self.funding_rate.current_rate,
                "avg_24h": self.funding_rate.avg_rate_24h,
                "trend": self.funding_rate.trend,
                "extremity": self.funding_rate.extremity,
                "interpretation": self.funding_rate.interpretation,
            }

        if self.open_interest:
            result["open_interest"] = {
                "current": self.open_interest.current_oi,
                "change_24h_pct": self.open_interest.oi_change_24h_pct,
                "divergence": self.open_interest.price_oi_divergence,
                "interpretation": self.open_interest.interpretation,
            }

        if self.long_short_ratio:
            result["long_short_ratio"] = {
                "ratio": self.long_short_ratio.long_short_ratio,
                "long_pct": self.long_short_ratio.long_pct,
                "short_pct": self.long_short_ratio.short_pct,
                "sentiment": self.long_short_ratio.sentiment,
                "interpretation": self.long_short_ratio.interpretation,
            }

        if self.liquidation:
            result["liquidation"] = {
                "nearest_above": self.liquidation.nearest_cluster_above,
                "nearest_below": self.liquidation.nearest_cluster_below,
                "interpretation": self.liquidation.interpretation,
            }

        return result

    def generate_summary(self) -> str:
        """Generate human-readable summary text."""
        lines = ["=== 加密货币专用指标分析 ===", ""]

        if self.funding_rate:
            lines.append(f"📊 资金费率: {self.funding_rate.current_rate:.4f}")
            lines.append(f"   {self.funding_rate.interpretation}")
            lines.append("")

        if self.open_interest:
            lines.append(f"📈 持仓量变化: {self.open_interest.oi_change_24h_pct:+.2f}%")
            lines.append(f"   {self.open_interest.interpretation}")
            lines.append("")

        if self.long_short_ratio:
            lines.append(f"⚖️ 多空比: {self.long_short_ratio.long_short_ratio:.2f}")
            lines.append(f"   {self.long_short_ratio.interpretation}")
            lines.append("")

        if self.liquidation:
            lines.append(f"💥 清算分布:")
            lines.append(f"   {self.liquidation.interpretation}")
            lines.append("")

        if self.key_insights:
            lines.append("🔑 关键洞察:")
            for insight in self.key_insights:
                lines.append(f"   • {insight}")
            lines.append("")

        if self.risk_warnings:
            lines.append("⚠️ 风险警示:")
            for warning in self.risk_warnings:
                lines.append(f"   • {warning}")
            lines.append("")

        return "\n".join(lines)


def compute_crypto_indicators(
    funding_rates: Optional[List[float]] = None,
    current_oi: Optional[float] = None,
    oi_24h_ago: Optional[float] = None,
    oi_7d_ago: Optional[float] = None,
    price_change_24h_pct: float = 0.0,
    long_accounts: int = 0,
    short_accounts: int = 0,
    current_price: float = 0.0,
    liquidation_map: Optional[Dict[float, float]] = None,
) -> CryptoIndicatorsSummary:
    """Compute all crypto-specific indicators.

    Args:
        funding_rates: List of recent funding rates
        current_oi: Current open interest
        oi_24h_ago: Open interest 24h ago
        oi_7d_ago: Open interest 7d ago
        price_change_24h_pct: 24h price change percentage
        long_accounts: Number of long positions
        short_accounts: Number of short positions
        current_price: Current BTC price
        liquidation_map: Liquidation heatmap data

    Returns:
        CryptoIndicatorsSummary with all analyses
    """
    summary = CryptoIndicatorsSummary()

    # Analyze funding rate
    if funding_rates:
        summary.funding_rate = FundingRateAnalysis.analyze(funding_rates)
        if summary.funding_rate.extremity in ["extreme_long", "extreme_short"]:
            summary.risk_warnings.append(summary.funding_rate.interpretation)

    # Analyze open interest
    if current_oi and oi_24h_ago and oi_7d_ago:
        summary.open_interest = OpenInterestAnalysis.analyze(
            current_oi, oi_24h_ago, oi_7d_ago, price_change_24h_pct
        )
        if abs(summary.open_interest.oi_change_24h_pct) > 10:
            summary.key_insights.append(
                f"持仓量24h变化{summary.open_interest.oi_change_24h_pct:+.1f}%，市场参与度显著变化"
            )

    # Analyze long/short ratio
    if long_accounts > 0 or short_accounts > 0:
        summary.long_short_ratio = LongShortRatioAnalysis.analyze(long_accounts, short_accounts)
        if summary.long_short_ratio.sentiment in ["extreme_greed", "extreme_fear"]:
            summary.risk_warnings.append(summary.long_short_ratio.interpretation)

    # Analyze liquidations
    if liquidation_map and current_price > 0:
        summary.liquidation = LiquidationAnalysis.analyze(current_price, liquidation_map)
        if summary.liquidation.nearest_cluster_above or summary.liquidation.nearest_cluster_below:
            summary.key_insights.append(summary.liquidation.interpretation)

    # Determine overall sentiment
    sentiments = []
    if summary.funding_rate:
        if summary.funding_rate.extremity == "extreme_long":
            sentiments.append(-1)  # Bearish
        elif summary.funding_rate.extremity == "extreme_short":
            sentiments.append(1)  # Bullish

    if summary.long_short_ratio:
        if summary.long_short_ratio.sentiment in ["extreme_greed", "greed"]:
            sentiments.append(-1)  # Bearish (contrarian)
        elif summary.long_short_ratio.sentiment in ["extreme_fear", "fear"]:
            sentiments.append(1)  # Bullish (contrarian)

    if sentiments:
        avg_sentiment = sum(sentiments) / len(sentiments)
        if avg_sentiment > 0.5:
            summary.overall_sentiment = "bullish"
        elif avg_sentiment < -0.5:
            summary.overall_sentiment = "bearish"
        else:
            summary.overall_sentiment = "neutral"

    return summary
