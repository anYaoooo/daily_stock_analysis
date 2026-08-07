from types import SimpleNamespace
from unittest.mock import patch

from src.analyzer import AnalysisResult, GeminiAnalyzer, align_btc_execution_plans


def _runtime_config() -> SimpleNamespace:
    return SimpleNamespace(
        crypto_backtest_engine_version="btc-plan-v5",
        crypto_backtest_neutral_band_pct=0.2,
        crypto_backtest_initial_equity=10000,
        crypto_backtest_risk_per_trade_pct=1,
        crypto_backtest_max_notional_pct=100,
        crypto_backtest_leverage=1,
        crypto_backtest_fee_rate_bps=5,
        crypto_backtest_slippage_bps=2,
        crypto_backtest_maker_fee_rate_bps=2,
        crypto_backtest_taker_fee_rate_bps=5,
        crypto_backtest_maintenance_margin_rate=0.005,
        crypto_backtest_minimum_risk_reward=1.2,
        crypto_backtest_minimum_volume_ratio=1.0,
    )


def _contract() -> dict:
    return {
        "version": "btc-execution-v1",
        "entry": {
            "logic": "all",
            "conditions": [
                {"type": "close_above", "value": 100},
                {"type": "volume_ratio_gte", "value": 1.0},
            ],
            "confirmation_bars": 1,
            "fill": "next_bar_open",
            "max_wait_bars": 3,
        },
        "exit": {"max_holding_bars": 5},
    }


def test_invalid_btc_trade_plan_is_annotated_not_downgraded() -> None:
    result = AnalysisResult(
        code="BTCUSDT",
        name="Bitcoin",
        sentiment_score=70,
        trend_prediction="看多",
        report_language="zh",
        operation_advice="买入",
        decision_type="buy",
        dashboard={
            "core_conclusion": {"one_sentence": "建议买入"},
            "battle_plan": {
                "long_plan": {
                    "direction": "long",
                    "entry_price": 100,
                    "stop_loss": 95,
                    "take_profit": 105,
                    "execution_contract": _contract(),
                },
                "short_plan": {"direction": "wait", "no_trade_reason": "等待跌破确认"},
            },
        },
    )

    aligned = align_btc_execution_plans(result, runtime_config=_runtime_config())

    long_plan = aligned.dashboard["battle_plan"]["long_plan"]
    assert long_plan["direction"] == "long"
    assert long_plan["validation_status"] == "failed"
    assert "risk_reward_below_minimum" in long_plan["validation_errors"]
    assert "risk_reward_below_minimum" in long_plan["validation_note"]
    short_plan = aligned.dashboard["battle_plan"]["short_plan"]
    assert short_plan["direction"] == "wait"
    assert short_plan["validation_status"] == "skipped"
    assert aligned.operation_advice == "买入"
    assert aligned.decision_type == "buy"


def test_valid_btc_trade_plan_keeps_directional_advice() -> None:
    result = AnalysisResult(
        code="BTCUSDT",
        name="Bitcoin",
        sentiment_score=70,
        trend_prediction="看多",
        report_language="zh",
        operation_advice="买入",
        decision_type="buy",
        dashboard={
            "battle_plan": {
                "long_plan": {
                    "direction": "long",
                    "entry_price": 100,
                    "stop_loss": 95,
                    "take_profit": 110,
                    "execution_contract": _contract(),
                }
            }
        },
    )

    aligned = align_btc_execution_plans(result, runtime_config=_runtime_config())

    long_plan = aligned.dashboard["battle_plan"]["long_plan"]
    assert long_plan["direction"] == "long"
    assert long_plan["validation_status"] == "passed"
    assert long_plan["validation_errors"] == []
    assert aligned.operation_advice == "买入"
    assert aligned.decision_type == "buy"


def test_execution_ladder_mismatch_is_annotated_on_plan() -> None:
    result = AnalysisResult(
        code="BTCUSDT",
        name="Bitcoin",
        sentiment_score=70,
        trend_prediction="看多",
        report_language="zh",
        operation_advice="买入",
        decision_type="buy",
        dashboard={
            "battle_plan": {
                "long_plan": {
                    "direction": "long",
                    "entry_price": 100,
                    "stop_loss": 95,
                    "take_profit": 110,
                    "execution_contract": _contract(),
                    "execution_ladder": {
                        "scenario": "trend_pullback",
                        "current_action": "trial",
                        "trial_entry": {"entry_price": 101},
                        "confirmation_add": {"entry_price": 104},
                        "invalidation": {"price": 94},
                    },
                },
            },
        },
    )

    aligned = align_btc_execution_plans(result, runtime_config=_runtime_config())

    plan = aligned.dashboard["battle_plan"]["long_plan"]
    assert plan["direction"] == "long"
    assert plan["validation_status"] == "failed"
    assert "execution_ladder_trial_entry_price_mismatch" in plan["validation_errors"]
    assert "execution_ladder_invalidation_price_mismatch" in plan["validation_errors"]


def test_validation_failure_preserves_original_advice() -> None:
    result = AnalysisResult(
        code="BTCUSDT",
        name="Bitcoin",
        sentiment_score=30,
        trend_prediction="看空",
        report_language="zh",
        operation_advice="减仓",
        decision_type="sell",
        action="reduce",
        dashboard={
            "battle_plan": {
                "short_plan": {
                    "direction": "short",
                    "entry_price": 100,
                    "stop_loss": 105,
                    # Missing take_profit -> missing_exit_prices
                    "execution_contract": _contract(),
                }
            }
        },
    )

    aligned = align_btc_execution_plans(result, runtime_config=_runtime_config())

    plan = aligned.dashboard["battle_plan"]["short_plan"]
    assert plan["direction"] == "short"
    assert plan["validation_status"] == "failed"
    assert "missing_exit_prices" in plan["validation_errors"]
    assert aligned.operation_advice == "减仓"
    assert aligned.decision_type == "sell"
    assert aligned.action == "reduce"


def test_late_volatility_trigger_disables_immediate_intraday_entry() -> None:
    result = AnalysisResult(
        code="BTCUSDT",
        name="Bitcoin",
        sentiment_score=70,
        trend_prediction="看多",
        report_language="zh",
        operation_advice="买入",
        decision_type="buy",
        dashboard={
            "battle_plan": {
                "intraday_plan": {
                    "enabled": True,
                    "direction": "long",
                    "entry_price": 102.3,
                    "execution_ladder": {
                        "current_action": "trial",
                        "trial_entry": {"enabled": True, "entry_price": 102.3},
                    },
                }
            }
        },
    )

    aligned = align_btc_execution_plans(
        result,
        runtime_config=_runtime_config(),
        trigger_context={
            "entry_executable_now": 0,
            "impulse_stage": "late_extension",
            "price": 102.3,
            "no_chase_price": 101.7,
        },
    )

    plan = aligned.dashboard["battle_plan"]["intraday_plan"]
    assert plan["enabled"] is False
    assert plan["direction"] == "wait"
    assert plan["trigger_execution_state"] == "late_extension"
    assert "不具备可执行试仓条件" in plan["no_trade_reason"]
    assert plan["execution_ladder"]["current_action"] == "wait"
    assert plan["execution_ladder"]["trial_entry"]["enabled"] is False


def test_analyze_aligns_execution_plans_for_crypto_context() -> None:
    analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
    config = SimpleNamespace(
        gemini_request_delay=0,
        report_language="zh",
        litellm_model="gemini/gemini-2.0-flash",
        llm_temperature=0.2,
        report_integrity_enabled=False,
        report_integrity_retry=0,
    )
    analyzer._config_override = config
    parsed_result = AnalysisResult(
        code="BTCUSDT",
        name="Bitcoin",
        sentiment_score=80,
        trend_prediction="看多",
        operation_advice="持有",
        analysis_summary="分析结果",
    )

    with patch.object(analyzer, "is_available", return_value=True), \
         patch.object(analyzer, "_get_analysis_system_prompt", return_value="system"), \
         patch.object(analyzer, "_format_prompt", return_value="prompt"), \
         patch.object(analyzer, "_call_litellm", return_value=("response", "model", {})), \
         patch.object(analyzer, "_parse_response", return_value=parsed_result), \
         patch.object(analyzer, "_build_market_snapshot", return_value={}), \
         patch("src.analyzer.persist_llm_usage"), \
         patch("src.analyzer.align_btc_execution_plans") as mock_align:
        result = analyzer.analyze({"code": "BTCUSDT", "stock_name": "Bitcoin", "market": "crypto"})

    assert result is parsed_result
    mock_align.assert_called_once_with(
        parsed_result,
        runtime_config=config,
        trigger_context=None,
    )
