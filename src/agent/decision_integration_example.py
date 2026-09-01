# -*- coding: utf-8 -*-
"""
P0 Fix Integration Example - Decision Weights & Crypto Indicators

This file demonstrates how to integrate:
1. Decision weights (P0-3) into DecisionAgent
2. Crypto indicators (P0-4) into TechnicalAgent

Copy the relevant code snippets into the actual agent files.
"""

from typing import Dict, Any, Optional
import logging

from src.agent.decision_weights import DecisionWeights, load_weights_from_config
from src.indicators.crypto_indicators import (
    compute_crypto_indicators,
    CryptoIndicatorsSummary,
)

logger = logging.getLogger(__name__)


# ============================================================================
# P0-3: Decision Weights Integration for DecisionAgent
# ============================================================================

def integrate_decision_weights_example(ctx, opinions) -> Dict[str, Any]:
    """
    Example of how to integrate decision weights into DecisionAgent.

    Add this logic to DecisionAgent.post_process() or build_user_message()
    """
    # 1. Load decision weights from configuration
    weights = load_weights_from_config(ctx.meta.get("config"))

    # 2. Extract scores from agent opinions
    technical_score = 50.0
    fundamental_score = 50.0
    sentiment_score = 50.0
    risk_score = 50.0

    for opinion in opinions:
        agent_name = opinion.agent_name.lower()
        confidence = opinion.confidence * 100  # Convert to 0-100 scale

        if "technical" in agent_name:
            technical_score = confidence
        elif "intel" in agent_name:
            sentiment_score = confidence
        elif "risk" in agent_name:
            risk_score = confidence

    # 3. Compute weighted final score
    weighted_result = weights.compute_weighted_score(
        technical_score=technical_score,
        fundamental_score=fundamental_score,
        sentiment_score=sentiment_score,
        risk_score=risk_score,
    )

    # 4. Log weights and contributions for transparency
    logger.info(f"Decision weights applied: {weights}")
    logger.info(f"Technical contribution: {weighted_result['technical_contribution']:.2f}")
    logger.info(f"Fundamental contribution: {weighted_result['fundamental_contribution']:.2f}")
    logger.info(f"Sentiment contribution: {weighted_result['sentiment_contribution']:.2f}")
    logger.info(f"Risk contribution: {weighted_result['risk_contribution']:.2f}")
    logger.info(f"Final weighted score: {weighted_result['final_score']:.2f}")

    # 5. Return weighted result with metadata
    return {
        "weighted_score": weighted_result["final_score"],
        "weight_breakdown": {
            "technical": weighted_result["technical_contribution"],
            "fundamental": weighted_result["fundamental_contribution"],
            "sentiment": weighted_result["sentiment_contribution"],
            "risk": weighted_result["risk_contribution"],
        },
        "weights_config": weights.to_dict(),
    }


# ============================================================================
# P0-4: Crypto Indicators Integration for TechnicalAgent
# ============================================================================

def integrate_crypto_indicators_example(
    ctx,
    current_price: float,
    price_change_24h_pct: float,
) -> Optional[CryptoIndicatorsSummary]:
    """
    Example of how to integrate crypto indicators into TechnicalAgent.

    Add this logic to TechnicalAgent.build_user_message() or system_prompt()
    """
    # 1. Check if crypto indicators are enabled
    config = ctx.meta.get("config")
    if not getattr(config, "btc_crypto_indicators_enabled", False):
        logger.debug("Crypto indicators disabled, skipping")
        return None

    # 2. Fetch crypto-specific data (this should come from crypto_fetcher)
    # These are example values - replace with actual data fetching
    funding_rates_sample = [0.0001, 0.0002, 0.00015, 0.00012]  # Recent funding rates
    current_oi_sample = 5000000000.0  # Current open interest
    oi_24h_ago_sample = 4800000000.0  # OI 24h ago
    oi_7d_ago_sample = 4500000000.0   # OI 7d ago
    long_accounts_sample = 55000       # Long position accounts
    short_accounts_sample = 45000      # Short position accounts
    liquidation_map_sample = {
        current_price * 1.02: 500000000,  # Liquidation cluster 2% above
        current_price * 0.98: 600000000,  # Liquidation cluster 2% below
    }

    # 3. Compute crypto indicators
    indicators = compute_crypto_indicators(
        funding_rates=funding_rates_sample,
        current_oi=current_oi_sample,
        oi_24h_ago=oi_24h_ago_sample,
        oi_7d_ago=oi_7d_ago_sample,
        price_change_24h_pct=price_change_24h_pct,
        long_accounts=long_accounts_sample,
        short_accounts=short_accounts_sample,
        current_price=current_price,
        liquidation_map=liquidation_map_sample,
    )

    # 4. Log indicator analysis
    logger.info(f"Crypto indicators computed: {indicators.overall_sentiment}")
    if indicators.funding_rate:
        logger.info(f"Funding rate: {indicators.funding_rate.current_rate:.4f} ({indicators.funding_rate.extremity})")
    if indicators.long_short_ratio:
        logger.info(f"Long/Short ratio: {indicators.long_short_ratio.long_short_ratio:.2f}")

    # 5. Return indicators for inclusion in analysis
    return indicators


# ============================================================================
# Integration Instructions
# ============================================================================

def print_integration_instructions():
    """Print step-by-step integration instructions."""
    print("""
=============================================================================
P0 FIXES - CODE INTEGRATION INSTRUCTIONS
=============================================================================

P0-3: Decision Weights Integration
-----------------------------------
File: src/agent/agents/decision_agent.py

1. Add import at top:
   from src.agent.decision_weights import DecisionWeights, load_weights_from_config

2. Add to DecisionAgent.__init__():
   self._weights: Optional[DecisionWeights] = None

3. Add method to load weights:
   def _load_decision_weights(self, ctx: AgentContext) -> DecisionWeights:
       if self._weights is None:
           config = ctx.meta.get("config")
           self._weights = load_weights_from_config(config) if config else DecisionWeights()
       return self._weights

4. In build_user_message(), add weights info to context:
   weights = self._load_decision_weights(ctx)
   parts.append(f"## Decision Weights Configuration")
   parts.append(f"- Technical: {weights.technical:.2f}")
   parts.append(f"- Fundamental: {weights.fundamental:.2f}")
   parts.append(f"- Sentiment: {weights.sentiment:.2f}")
   parts.append(f"- Risk: {weights.risk:.2f}")

5. In post_process(), compute and log weighted score:
   weights = self._load_decision_weights(ctx)
   weighted_result = weights.compute_weighted_score(...)
   dashboard["weight_breakdown"] = weighted_result["weight_breakdown"]
   dashboard["weights_used"] = weights.to_dict()

-----------------------------------

P0-4: Crypto Indicators Integration
-----------------------------------
File: src/agent/agents/technical_agent.py

1. Add import at top:
   from src.indicators.crypto_indicators import compute_crypto_indicators

2. In build_user_message(), add crypto indicators section:
   config = ctx.meta.get("config")
   if getattr(config, "btc_crypto_indicators_enabled", False):
       indicators = compute_crypto_indicators(...)
       if indicators:
           parts.append("## Crypto-Specific Indicators")
           parts.append(indicators.generate_summary())

3. Include crypto indicators in analysis prompt:
   - Funding rate analysis
   - Open interest divergence
   - Long/short ratio sentiment
   - Liquidation cluster levels

-----------------------------------

Testing After Integration
-----------------------------------
1. Run decision weights tests:
   pytest tests/test_decision_weights.py -v

2. Test with actual BTC analysis:
   python -m src.analyzer --stock BTC --config .env

3. Check logs for:
   - "Loaded decision weights: DecisionWeights(...)"
   - "Decision weights applied: ..."
   - "Crypto indicators computed: ..."

4. Verify dashboard JSON includes:
   - weight_breakdown
   - weights_used
   - crypto_indicators (if enabled)

=============================================================================
""")


if __name__ == "__main__":
    print_integration_instructions()
