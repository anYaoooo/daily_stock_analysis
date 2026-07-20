from types import SimpleNamespace

from src.analyzer import AnalysisResult, align_btc_execution_plans


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


def test_invalid_btc_trade_plan_is_downgraded_to_watch() -> None:
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
    assert long_plan["direction"] == "wait"
    assert "risk_reward_below_minimum" in long_plan["no_trade_reason"]
    assert aligned.operation_advice == "观望，等待可执行交易条件"
    assert aligned.decision_type == "hold"
    assert aligned.action == "watch"


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

    assert aligned.dashboard["battle_plan"]["long_plan"]["direction"] == "long"
    assert aligned.operation_advice == "买入"
    assert aligned.decision_type == "buy"
