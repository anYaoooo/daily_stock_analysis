from types import SimpleNamespace

from src.utils.sniper_points import extract_directional_strategy_plans


def test_extract_directional_strategy_plans_from_dashboard() -> None:
    result = SimpleNamespace(
        dashboard={
            "battle_plan": {
                "long_plan": {
                    "entry_price": "多单入场：100000",
                    "stop_loss": "99000",
                    "take_profit": "103000",
                    "trigger_condition": "突破确认",
                    "invalidation": "跌回突破位",
                    "reason": "VWAP 上方且放量",
                    "execution_contract": {
                        "version": "btc-execution-v1",
                        "entry": {
                            "logic": "all",
                            "conditions": [{"type": "close_above", "value": 100000}],
                            "confirmation_bars": 1,
                            "fill": "next_bar_open",
                            "max_wait_bars": 3,
                        },
                        "exit": {"max_holding_bars": 5},
                    },
                    "execution_ladder": {
                        "scenario": "trend_pullback",
                        "current_action": "trial",
                        "trial_entry": {"entry_price": 100000, "trigger_condition": "回踩承接"},
                        "confirmation_add": {"entry_price": 100800, "trigger_condition": "重新站上前高"},
                        "invalidation": {"price": 99000, "condition": "跌破回踩低点"},
                    },
                },
                "short_plan": {
                    "entry_price": "空单入场：98000",
                    "stop_loss": "99500",
                    "take_profit": "95000",
                    "trigger_condition": "跌破支撑",
                    "invalidation": "重新站回支撑",
                    "reason": "跌破且量能确认",
                },
                "intraday_plan": {
                    "enabled": True,
                    "direction": "long",
                    "entry_price": "小时线入场：100500",
                    "stop_loss": "99800",
                    "take_profit": "102000",
                    "trigger_condition": "小时线突破前高",
                    "invalidation": "跌回小时线 VWAP 下方",
                    "daily_constraint": "必须守住日线支撑 99000",
                    "reason": "小时线与日线同向",
                },
            }
        }
    )

    plans = extract_directional_strategy_plans(result)

    assert plans["long_plan"]["entry_price"] == "多单入场：100000"
    assert plans["long_plan"]["trigger_condition"] == "突破确认"
    assert plans["long_plan"]["execution_contract"]["entry"]["conditions"][0]["value"] == 100000
    assert plans["long_plan"]["execution_ladder"]["trial_entry"]["entry_price"] == 100000
    assert plans["short_plan"]["entry_price"] == "空单入场：98000"
    assert plans["short_plan"]["invalidation"] == "重新站回支撑"
    assert plans["intraday_plan"]["enabled"] is True
    assert plans["intraday_plan"]["direction"] == "long"
    assert plans["intraday_plan"]["daily_constraint"] == "必须守住日线支撑 99000"


def test_extract_directional_strategy_plans_from_strategy_payload() -> None:
    plans = extract_directional_strategy_plans(
        {
            "long_plan": {"entry_price": "100000", "take_profit": "103000"},
            "short_plan": {"entry_price": "98000", "take_profit": "95000"},
            "intraday_plan": {"enabled": False, "direction": "wait", "reason": "小时线未触发"},
        }
    )

    assert plans["long_plan"]["entry_price"] == "100000"
    assert plans["short_plan"]["take_profit"] == "95000"
    assert plans["intraday_plan"]["direction"] == "wait"
