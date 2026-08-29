from types import SimpleNamespace
from unittest.mock import patch

from src.analyzer import AnalysisResult, GeminiAnalyzer, align_btc_execution_plans
from src.services.crypto_backtest_service import build_btc_mfe_calibration


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


def test_mfe_calibration_uses_completed_triggered_rows_by_horizon() -> None:
    rows = [
        SimpleNamespace(eval_status="completed", signal_triggered=True, horizon="intraday", mfe_pct=0.4),
        SimpleNamespace(eval_status="completed", signal_triggered=True, horizon="intraday", mfe_pct=0.8),
        SimpleNamespace(eval_status="completed", signal_triggered=True, horizon="intraday", mfe_pct=1.2),
        SimpleNamespace(eval_status="completed", signal_triggered=False, horizon="intraday", mfe_pct=9.0),
        SimpleNamespace(eval_status="skipped", signal_triggered=True, horizon="daily", mfe_pct=9.0),
    ]

    calibration = build_btc_mfe_calibration(rows)

    assert calibration["intraday"]["sample_count"] == 3
    assert calibration["intraday"]["mfe_p50_pct"] == 0.8
    assert calibration["intraday"]["mfe_p70_pct"] == 0.96
    assert "daily" not in calibration


def test_mfe_calibration_marks_target_beyond_p70_without_downgrading() -> None:
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

    aligned = align_btc_execution_plans(
        result,
        runtime_config=_runtime_config(),
        mfe_calibration={
            "daily": {"sample_count": 20, "mfe_p50_pct": 0.5, "mfe_p70_pct": 2.0},
            "intraday": {"sample_count": 20, "mfe_p50_pct": 0.5, "mfe_p70_pct": 2.0},
        },
    )

    plan = aligned.dashboard["battle_plan"]["long_plan"]
    assert plan["target_calibration"]["status"] == "beyond_typical_mfe"
    assert plan["validation_status"] == "passed"


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


def test_confirmation_price_must_preserve_minimum_risk_reward() -> None:
    contract = _contract()
    contract["entry"]["conditions"][0]["value"] = 108
    result = AnalysisResult(
        code="BTC",
        name="Bitcoin",
        sentiment_score=70,
        trend_prediction="看多",
        report_language="zh",
        operation_advice="等待回踩",
        dashboard={
            "battle_plan": {
                "long_plan": {
                    "direction": "long",
                    "entry_price": 100,
                    "stop_loss": 95,
                    "take_profit": 110,
                    "risk_reward": "1:9.99（模型误算）",
                    "execution_contract": contract,
                }
            }
        },
    )

    aligned = align_btc_execution_plans(result, runtime_config=_runtime_config())

    plan = aligned.dashboard["battle_plan"]["long_plan"]
    assert plan["validation_status"] == "failed"
    assert "risk_reward_below_minimum_at_confirmation" in plan["validation_errors"]
    assert plan["calculated_risk_reward"] == 2.0
    assert plan["risk_reward"] == "1:2.00（按入场 100、止损 95、止盈 110 重新计算）"


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


def test_liquidity_sweep_guard_disables_stale_chase_plan() -> None:
    result = AnalysisResult(
        code="BTCUSDT",
        name="Bitcoin",
        sentiment_score=50,
        trend_prediction="看空",
        report_language="zh",
        operation_advice="观望",
        decision_type="hold",
        dashboard={
            "battle_plan": {
                "intraday_plan": {
                    "enabled": True,
                    "direction": "short",
                    "execution_ladder": {
                        "current_action": "trial",
                        "trial_entry": {"enabled": True, "entry_price": 101.8},
                    },
                }
            }
        },
    )

    aligned = align_btc_execution_plans(
        result,
        runtime_config=_runtime_config(),
        trigger_context={
            "trigger_reason": "liquidity_sweep",
            "right_side_direction": "short",
            "price": 100.0,
        },
        technical_context={
            "timeframes": {
                "hourly": {
                    "event": {
                        "right_side": {
                            "version": "btc-right-side-v1",
                            "state": "sweep_detected",
                            "direction": "short",
                            "no_chase_price": 101.7,
                        }
                    }
                }
            }
        },
    )

    plan = aligned.dashboard["battle_plan"]["intraday_plan"]
    assert plan["enabled"] is False
    assert plan["direction"] == "wait"
    assert plan["trigger_execution_state"] == "right_side_missed"
    assert "机会已错过" in plan["no_trade_reason"]
    assert plan["execution_ladder"]["current_action"] == "wait"
    assert plan["execution_ladder"]["trial_entry"]["enabled"] is False


def test_aligned_timeframes_block_opposed_intraday_plan() -> None:
    result = AnalysisResult(
        code="BTCUSDT",
        name="Bitcoin",
        sentiment_score=50,
        trend_prediction="看多",
        report_language="zh",
        operation_advice="观望",
        dashboard={
            "battle_plan": {
                "intraday_plan": {
                    "enabled": True,
                    "direction": "short",
                    "entry_price": 100,
                    "execution_ladder": {
                        "current_action": "trial",
                        "trial_entry": {"enabled": True, "entry_price": 100},
                    },
                }
            }
        },
    )

    aligned = align_btc_execution_plans(
        result,
        runtime_config=_runtime_config(),
        technical_context={"intraday": {"alignment": "aligned_long"}},
    )

    plan = aligned.dashboard["battle_plan"]["intraday_plan"]
    assert plan["enabled"] is False
    assert plan["direction"] == "wait"
    assert plan["direction_guard"] == "blocked_by_aligned_timeframes"
    assert plan["multi_timeframe_alignment"] == "aligned_long"
    assert "日线与小时线均偏多" in plan["no_trade_reason"]
    assert plan["execution_ladder"]["current_action"] == "wait"
    assert plan["execution_ladder"]["trial_entry"]["enabled"] is False


def test_bearish_push_below_vwap_blocks_long_plan() -> None:
    result = AnalysisResult(
        code="BTCUSDT",
        name="Bitcoin",
        sentiment_score=60,
        trend_prediction="看多",
        report_language="zh",
        operation_advice="买入",
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

    aligned = align_btc_execution_plans(
        result,
        runtime_config=_runtime_config(),
        technical_context={
            "timeframes": {
                "daily": {
                    "price_action": {"state": "bearish_push"},
                    "vwap": {"price_position": "below"},
                    "volume": {"confirmation": "normal"},
                }
            }
        },
    )

    plan = aligned.dashboard["battle_plan"]["long_plan"]
    assert plan["enabled"] is False
    assert plan["direction"] == "wait"
    assert plan["tradeability_status"] == "blocked"
    assert plan["tradeability_reasons"] == ["long_bearish_push_below_vwap"]
    assert plan["tradeability_audit"]["version"] == "btc-tradeability-v1"
    assert plan["tradeability_audit"]["decision"] == "blocked"
    assert plan["tradeability_audit"]["original_plan"]["direction"] == "long"
    assert plan["tradeability_audit"]["original_plan"]["execution_contract"] == _contract()
    assert plan["validation_status"] == "skipped"


def test_low_volume_without_breakout_blocks_long_plan() -> None:
    result = AnalysisResult(
        code="BTCUSDT",
        name="Bitcoin",
        sentiment_score=60,
        trend_prediction="看多",
        report_language="zh",
        operation_advice="买入",
        dashboard={
            "battle_plan": {
                "intraday_plan": {
                    "enabled": True,
                    "direction": "long",
                    "entry_price": 100,
                    "stop_loss": 95,
                    "take_profit": 110,
                    "execution_contract": _contract(),
                }
            }
        },
    )

    aligned = align_btc_execution_plans(
        result,
        runtime_config=_runtime_config(),
        technical_context={
            "timeframes": {
                "hourly": {
                    "price_action": {"state": "range", "close_above_resistance": False},
                    "vwap": {"price_position": "above"},
                    "volume": {"confirmation": "low"},
                }
            }
        },
    )

    plan = aligned.dashboard["battle_plan"]["intraday_plan"]
    assert plan["direction"] == "wait"
    assert plan["tradeability_reasons"] == ["long_low_volume_without_close_breakout"]

    realigned = align_btc_execution_plans(
        aligned,
        runtime_config=_runtime_config(),
        technical_context={
            "timeframes": {
                "hourly": {
                    "price_action": {"state": "range", "close_above_resistance": False},
                    "vwap": {"price_position": "above"},
                    "volume": {"confirmation": "low"},
                }
            }
        },
    )
    realigned_plan = realigned.dashboard["battle_plan"]["intraday_plan"]
    assert realigned_plan["tradeability_audit"]["original_plan"]["direction"] == "long"


def test_countertrend_intraday_plan_is_capped_and_expires_early() -> None:
    contract = _contract()
    contract["entry"]["max_wait_bars"] = 24
    result = AnalysisResult(
        code="BTCUSDT",
        name="Bitcoin",
        sentiment_score=50,
        trend_prediction="震荡",
        report_language="zh",
        operation_advice="观望",
        dashboard={
            "battle_plan": {
                "intraday_plan": {
                    "enabled": True,
                    "direction": "long",
                    "entry_price": 100,
                    "stop_loss": 95,
                    "take_profit": 110,
                    "execution_contract": contract,
                }
            }
        },
    )

    aligned = align_btc_execution_plans(
        result,
        runtime_config=_runtime_config(),
        technical_context={
            "intraday": {
                "alignment": "countertrend_long",
                "daily_bias": "short",
                "hourly_bias": "long",
            },
            "derivatives": {
                "data_quality": "available",
                "order_flow": {"data_quality": "available"},
            },
            "macro_correlation": {"data_quality": "available"},
            "timeframes": {
                "hourly": {
                    "volatility": {
                        "forecast": {
                            "data_quality": "available",
                            "position_multiplier_cap": 1.0,
                        }
                    }
                }
            },
        },
    )

    plan = aligned.dashboard["battle_plan"]["intraday_plan"]
    assert plan["tradeability_status"] == "countertrend_limited"
    assert plan["position_multiplier_cap"] == 0.5
    assert "countertrend_position_cap_50pct" in plan["tradeability_reasons"]
    assert plan["countertrend_control"]["max_validity_bars"] == 6
    assert plan["execution_contract"]["entry"]["max_wait_bars"] == 6


def test_missing_context_degrades_position_cap_without_changing_direction() -> None:
    result = AnalysisResult(
        code="BTCUSDT",
        name="Bitcoin",
        sentiment_score=60,
        trend_prediction="看空",
        report_language="zh",
        operation_advice="卖出",
        dashboard={
            "battle_plan": {
                "short_plan": {
                    "direction": "short",
                    "entry_price": 100,
                    "stop_loss": 105,
                    "take_profit": 90,
                    "execution_contract": {
                        **_contract(),
                        "entry": {
                            **_contract()["entry"],
                            "conditions": [
                                {"type": "close_below", "value": 100},
                                {"type": "volume_ratio_gte", "value": 1.0},
                            ],
                        },
                    },
                }
            }
        },
    )

    aligned = align_btc_execution_plans(
        result,
        runtime_config=_runtime_config(),
        technical_context={"timeframes": {"daily": {}}},
    )

    plan = aligned.dashboard["battle_plan"]["short_plan"]
    assert plan["direction"] == "short"
    assert plan["tradeability_status"] == "degraded_missing_context"
    assert plan["position_multiplier_cap"] == 0.5
    assert "missing:volatility_forecast" in plan["tradeability_reasons"]
    assert plan["tradeability_audit"]["decision"] == "degraded"


def test_extreme_ewma_volatility_caps_plan_and_trial_position() -> None:
    result = AnalysisResult(
        code="BTCUSDT",
        name="Bitcoin",
        sentiment_score=70,
        trend_prediction="看多",
        report_language="zh",
        operation_advice="买入",
        dashboard={
            "battle_plan": {
                "long_plan": {
                    "direction": "long",
                    "entry_price": 100,
                    "stop_loss": 95,
                    "take_profit": 110,
                    "position_hint": "账户风险 1%",
                    "execution_contract": _contract(),
                    "execution_ladder": {
                        "current_action": "trial",
                        "trial_entry": {"entry_price": 100, "position_hint": "先试仓"},
                        "confirmation_add": {"entry_price": 103},
                        "invalidation": {"price": 95},
                    },
                }
            }
        },
    )

    aligned = align_btc_execution_plans(
        result,
        runtime_config=_runtime_config(),
        technical_context={
            "timeframes": {
                "daily": {
                    "volatility": {
                        "forecast": {
                            "data_quality": "available",
                            "model_version": "btc-ewma-vol-v1",
                            "forecast_sigma_pct": 4.2,
                            "historical_percentile": 96.0,
                            "regime": "extreme",
                            "position_multiplier_cap": 0.25,
                            "risk_action": "reduce_position_strongly",
                        }
                    }
                }
            }
        },
    )

    plan = aligned.dashboard["battle_plan"]["long_plan"]
    assert plan["direction"] == "long"
    assert plan["position_multiplier_cap"] == 0.25
    assert plan["risk_overlay"]["applied"] is True
    assert plan["risk_overlay"]["volatility_regime"] == "extreme"
    assert "原计划的 25%" in plan["position_hint"]
    assert plan["execution_ladder"]["trial_entry"]["position_multiplier_cap"] == 0.25
    assert "原计划的 25%" in plan["execution_ladder"]["trial_entry"]["position_hint"]


def test_ewma_overlay_preserves_stricter_existing_position_cap() -> None:
    result = AnalysisResult(
        code="BTCUSDT",
        name="Bitcoin",
        sentiment_score=70,
        trend_prediction="看多",
        report_language="zh",
        operation_advice="买入",
        dashboard={
            "battle_plan": {
                "long_plan": {
                    "direction": "long",
                    "entry_price": 100,
                    "stop_loss": 95,
                    "take_profit": 110,
                    "position_multiplier_cap": 0.25,
                    "execution_contract": _contract(),
                    "execution_ladder": {
                        "current_action": "trial",
                        "trial_entry": {"entry_price": 100},
                        "confirmation_add": {"entry_price": 103},
                        "invalidation": {"price": 95},
                    },
                }
            }
        },
    )

    aligned = align_btc_execution_plans(
        result,
        runtime_config=_runtime_config(),
        technical_context={
            "timeframes": {
                "daily": {
                    "volatility": {
                        "forecast": {
                            "data_quality": "available",
                            "model_version": "btc-ewma-vol-v1",
                            "forecast_sigma_pct": 2.5,
                            "historical_percentile": 80.0,
                            "regime": "elevated",
                            "position_multiplier_cap": 0.5,
                            "risk_action": "reduce_position",
                        }
                    }
                }
            }
        },
    )

    plan = aligned.dashboard["battle_plan"]["long_plan"]
    assert plan["position_multiplier_cap"] == 0.25
    assert plan["risk_overlay"]["source_position_multiplier_cap"] == 0.5
    assert plan["risk_overlay"]["binding"] is False
    assert plan["execution_ladder"]["trial_entry"]["position_multiplier_cap"] == 0.25
    assert "保留更严格" in plan["risk_overlay"]["note"]


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
        technical_context=None,
    )
