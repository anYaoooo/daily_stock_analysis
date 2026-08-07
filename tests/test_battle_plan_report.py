# -*- coding: utf-8 -*-
"""Tests for shared BTC battle-plan report rendering helpers."""

from src.utils.battle_plan_report import (
    has_directional_plans,
    render_directional_plan_overview,
    render_execution_ladders,
    render_intraday_plan_detail,
)


def test_overview_is_empty_without_directional_plans() -> None:
    assert render_directional_plan_overview({}) == []
    assert render_directional_plan_overview({"sniper_points": {"ideal_buy": "100"}}) == []
    assert has_directional_plans({"sniper_points": {}}) is False


def test_overview_renders_plans_with_validation_markers() -> None:
    battle = {
        "long_plan": {
            "direction": "long",
            "entry_price": 100000,
            "stop_loss": 98000,
            "take_profit": 105000,
            "trigger_condition": "突破确认",
            "validation_status": "passed",
        },
        "short_plan": {
            "direction": "short",
            "entry_price": 96000,
            "validation_status": "failed",
            "validation_note": "未通过执行校验（risk_reward_below_minimum），计划仅供参考。",
        },
        "intraday_plan": {"direction": "wait", "validation_status": "skipped"},
    }

    lines = render_directional_plan_overview(battle)
    text = "\n".join(lines)

    assert "BTC 双向计划概览" in text
    assert "✅校验通过" in text
    assert "⚠️未通过校验" in text
    assert "⏸️等待" in text
    # failed/skipped notes are surfaced for user judgement
    assert "未通过执行校验（risk_reward_below_minimum）" in text
    # passed plans should not add a note line
    assert not any(line.startswith("- 日线多单") for line in lines)


def test_overview_handles_missing_validation_fields() -> None:
    lines = render_directional_plan_overview({"long_plan": {"direction": "long"}})
    text = "\n".join(lines)
    assert "—" in text  # unknown validation status renders a neutral marker
    assert "N/A" in text


def test_intraday_detail_renders_validation_row() -> None:
    intraday = {
        "direction": "long",
        "entry_price": 100500,
        "stop_loss": 99800,
        "take_profit": 102000,
        "trigger_condition": "小时线收回 VWAP",
        "daily_constraint": "日线偏多",
        "reason": "回踩承接",
        "validation_status": "passed",
    }

    text = "\n".join(render_intraday_plan_detail(intraday))
    assert "小时线日内计划" in text
    assert "执行校验 | ✅校验通过" in text
    assert "日线约束" in text

    assert render_intraday_plan_detail({}) == []


def test_execution_ladders_render_from_shared_helper() -> None:
    battle = {
        "long_plan": {
            "direction": "long",
            "execution_ladder": {
                "scenario": "trend_pullback",
                "current_action": "trial",
                "trial_entry": {
                    "enabled": True,
                    "entry_price": 98000,
                    "trigger_condition": "回踩承接",
                    "position_hint": "0.25% 试仓",
                },
                "confirmation_add": {
                    "enabled": True,
                    "entry_price": 99200,
                    "trigger_condition": "收盘站上 99200",
                },
                "invalidation": {
                    "price": 97200,
                    "condition": "跌破 97200",
                    "action": "撤销试仓",
                },
            },
        },
    }

    text = "\n".join(render_execution_ladders(battle))
    assert "BTC 分步执行" in text
    assert "0.25% 试仓" in text
    assert "收盘站上 99200" in text
    assert render_execution_ladders({"long_plan": {"direction": "long"}}) == []
