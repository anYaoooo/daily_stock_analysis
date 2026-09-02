# -*- coding: utf-8 -*-
"""
TechnicalAgent — technical & price analysis specialist.

Responsible for:
- Fetching realtime quotes and historical K-line data
- Running technical indicators (trend, MA, volume, pattern)
- Producing a structured opinion on trend/momentum/support-resistance
"""

from __future__ import annotations

import logging
from typing import Optional

from src.agent.agents.base_agent import BaseAgent
from src.agent.protocols import AgentContext, AgentOpinion
from src.agent.runner import try_parse_json

logger = logging.getLogger(__name__)

# P0-4: Import crypto indicators module
try:
    from src.indicators.crypto_indicators import compute_crypto_indicators
    CRYPTO_INDICATORS_AVAILABLE = True
except ImportError:
    CRYPTO_INDICATORS_AVAILABLE = False
    logger.warning("Crypto indicators module not available, crypto-specific analysis disabled")


class TechnicalAgent(BaseAgent):
    agent_name = "technical"
    max_steps = 6
    tool_names = [
        "get_realtime_quote",
        "get_daily_history",
        "analyze_trend",
        "calculate_ma",
        "get_volume_analysis",
        "analyze_pattern",
        "get_chip_distribution",
        "get_analysis_context",
    ]

    def system_prompt(self, ctx: AgentContext) -> str:
        skills = ""
        if self.skill_instructions:
            skills = f"\n## Active Trading Skills\n\n{self.skill_instructions}\n"
        baseline = ""
        if self.technical_skill_policy:
            baseline = f"\n{self.technical_skill_policy}\n"
        crypto_guidance = ""
        if ctx.stock_code.upper() in ["BTC", "BTCUSDT", "BTC-USD", "BTC/USD", "BTCUSD"]:
            crypto_guidance = """
## BTC Direction Baseline
- BTC uses the pre-fetched `btc-direction-v2` baseline, not an equity buy-attractiveness score.
- `direction_score` is symmetric from -1 (strong bearish) to +1 (strong bullish); below +/-0.45, prefer wait/hold.
- RSI overbought/oversold and shrinking volume are risk/context signals only. They must not reverse a falling/rising BTC baseline by themselves.
- Keep long and short semantics symmetric and state the confirmation condition when evidence is mixed.
"""

        return f"""\
You are a **Technical Analysis Agent** specialising in Chinese A-shares, \
Hong Kong stocks, and US equities.

Your task: perform a thorough technical analysis of the given stock and \
output a structured JSON opinion.

## Workflow (execute stages in order)
1. Fetch realtime quote + daily history (if not already provided)
2. Run trend analysis (MA alignment, MACD, RSI)
3. Analyse volume and chip distribution
4. Identify chart patterns

## Trend interpretation
- When `[Pre-fetched: trend_result]` is present, treat its `price_trend` and
  `trend_status` as the deterministic technical baseline. Use `price_trend`
  to distinguish 上涨/下跌/震荡 and use the MA/MACD/RSI fields to explain it.
- Do not call a clear local `price_trend` of 上涨 or 下跌 震荡 merely because
  one indicator is neutral. If other evidence disagrees, describe the conflict
  and the confirmation condition instead of silently reversing the baseline.

{crypto_guidance}
{baseline}
{skills}
## Output Format
Return **only** a JSON object (no markdown fences):
{{
  "signal": "strong_buy|buy|hold|sell|strong_sell",
  "confidence": 0.0-1.0,
  "reasoning": "2-3 sentence summary",
  "key_levels": {{
    "support": <float>,
    "resistance": <float>,
    "stop_loss": <float>
  }},
  "trend_score": 0-100,
  "price_trend": "up|down|sideways",
  "ma_alignment": "bullish|neutral|bearish",
  "volume_status": "heavy|normal|light",
  "pattern": "<detected pattern or none>"
}}
"""

    def build_user_message(self, ctx: AgentContext) -> str:
        parts = [f"Perform technical analysis on stock **{ctx.stock_code}**"]
        if ctx.stock_name:
            parts[0] += f" ({ctx.stock_name})"

        # BTC derivatives are already fetched by the pipeline and stored in
        # crypto_technical. Never synthesize Funding/OI/position data here:
        # fixed demo values can silently override the real market context.
        if ctx.stock_code.upper() in ["BTC", "BTCUSDT", "BTC-USD", "BTC/USD", "BTCUSD"]:
            crypto_summary = self._get_crypto_indicators_summary(ctx)
            if crypto_summary:
                parts.append("\n## Crypto-Specific Market Indicators")
                parts.append(crypto_summary)
                parts.append("\nIncorporate these crypto-specific indicators into your technical analysis.")

        parts.append("\nUse your tools to fetch any missing data, then output the JSON opinion.")
        return "\n".join(parts)

    def _get_crypto_indicators_summary(self, ctx: AgentContext) -> Optional[str]:
        """Get crypto-specific indicators summary for BTC analysis.

        Returns:
            Formatted string with crypto indicators or None if unavailable
        """
        if not CRYPTO_INDICATORS_AVAILABLE:
            return None

        try:
            # Extract market data from context
            quote_data = ctx.get_data("realtime_quote")
            if not quote_data:
                logger.debug("No realtime quote data available for crypto indicators")
                return None

            current_price = float(quote_data.get("price", 0))
            if current_price <= 0:
                return None

            # Get 24h price change
            daily_history = ctx.get_data("daily_history")
            price_change_24h_pct = 0.0
            if daily_history and len(daily_history) >= 2:
                try:
                    prev_close = float(daily_history[-2].get("close", current_price))
                    if prev_close > 0:
                        price_change_24h_pct = ((current_price - prev_close) / prev_close) * 100
                except (KeyError, ValueError, IndexError):
                    pass

            crypto_context = ctx.get_data("crypto_technical")
            derivatives = crypto_context.get("derivatives") if isinstance(crypto_context, dict) else None
            if not isinstance(derivatives, dict):
                logger.debug("No actual BTC derivatives context available")
                return None

            # The orchestrator normally passes already-fetched derivatives in
            # the context, but it does not inject the full runtime config into
            # every AgentContext. A missing config is therefore not an
            # explicit opt-out. Only a present config with the switch set to
            # false disables this summary.
            config = ctx.meta.get("config")
            if (
                config is not None
                and hasattr(config, "btc_crypto_indicators_enabled")
                and not getattr(config, "btc_crypto_indicators_enabled")
            ):
                logger.debug("Crypto indicators disabled explicitly in config")
                return None

            funding = derivatives.get("funding") if isinstance(derivatives.get("funding"), dict) else {}
            open_interest = derivatives.get("open_interest") if isinstance(derivatives.get("open_interest"), dict) else {}
            long_short = derivatives.get("long_short_ratio") if isinstance(derivatives.get("long_short_ratio"), dict) else {}
            funding_rate = funding.get("rate")
            current_oi = open_interest.get("value")
            ratio = long_short.get("current")
            long_accounts = 0
            short_accounts = 0
            if isinstance(ratio, (int, float)) and float(ratio) > 0:
                # Account counts are not available from every venue. Using a
                # ratio-equivalent pair preserves the observed ratio without
                # pretending to know the absolute number of accounts.
                long_accounts = float(ratio)
                short_accounts = 1.0

            funding_rates = [float(funding_rate)] if isinstance(funding_rate, (int, float)) else None
            indicators = compute_crypto_indicators(
                funding_rates=funding_rates,
                current_oi=float(current_oi) if isinstance(current_oi, (int, float)) else None,
                price_change_24h_pct=price_change_24h_pct,
                long_accounts=long_accounts,
                short_accounts=short_accounts,
                current_price=current_price,
            )

            summary = indicators.generate_summary()
            logger.info(f"Crypto indicators computed for {ctx.stock_code}: {indicators.overall_sentiment}")
            return summary

        except Exception as exc:
            logger.warning(f"Failed to compute crypto indicators: {exc}", exc_info=True)
            return None

    def post_process(self, ctx: AgentContext, raw_text: str) -> Optional[AgentOpinion]:
        """Parse the JSON opinion from the LLM response."""
        parsed = try_parse_json(raw_text)
        if parsed is None:
            logger.warning("[TechnicalAgent] failed to parse opinion JSON")
            return None

        return AgentOpinion(
            agent_name=self.agent_name,
            signal=parsed.get("signal", "hold"),
            confidence=float(parsed.get("confidence", 0.5)),
            reasoning=parsed.get("reasoning", ""),
            key_levels={
                k: float(v) for k, v in parsed.get("key_levels", {}).items()
                if isinstance(v, (int, float))
            },
            raw_data=parsed,
        )
