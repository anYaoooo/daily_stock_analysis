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
                },
                "short_plan": {
                    "entry_price": "空单入场：98000",
                    "stop_loss": "99500",
                    "take_profit": "95000",
                    "trigger_condition": "跌破支撑",
                    "invalidation": "重新站回支撑",
                    "reason": "跌破且量能确认",
                },
            }
        }
    )

    plans = extract_directional_strategy_plans(result)

    assert plans["long_plan"]["entry_price"] == "多单入场：100000"
    assert plans["long_plan"]["trigger_condition"] == "突破确认"
    assert plans["short_plan"]["entry_price"] == "空单入场：98000"
    assert plans["short_plan"]["invalidation"] == "重新站回支撑"


def test_extract_directional_strategy_plans_from_strategy_payload() -> None:
    plans = extract_directional_strategy_plans(
        {
            "long_plan": {"entry_price": "100000", "take_profit": "103000"},
            "short_plan": {"entry_price": "98000", "take_profit": "95000"},
        }
    )

    assert plans["long_plan"]["entry_price"] == "100000"
    assert plans["short_plan"]["take_profit"] == "95000"
