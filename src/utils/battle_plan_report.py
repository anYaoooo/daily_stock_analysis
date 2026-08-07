# -*- coding: utf-8 -*-
"""Shared markdown rendering helpers for BTC directional battle plans.

Both the notification report and the history report renderer consume these
helpers so the two user-facing paths stay in sync. Every helper returns an
empty list when the battle plan has no BTC directional plans, so callers can
append unconditionally without affecting non-BTC reports.
"""

from typing import Any, Dict, List, Tuple

_VALIDATION_MARKERS = {
    "passed": "✅校验通过",
    "failed": "⚠️未通过校验",
    "skipped": "⏸️等待",
}

_PLAN_SLOTS: Tuple[Tuple[str, str], ...] = (
    ("日线多单", "long_plan"),
    ("日线空单", "short_plan"),
    ("小时线日内", "intraday_plan"),
)


def clean_plan_value(value: Any) -> str:
    """Normalize plan field values for markdown table display."""
    if value is None:
        return "N/A"
    text = str(value).strip()
    if not text or text in ("-", "—", "N/A", "None"):
        return "N/A"
    return text


def has_directional_plans(battle_plan: Dict[str, Any]) -> bool:
    """Return True when the battle plan carries BTC long/short plans."""
    if not isinstance(battle_plan, dict):
        return False
    return any(isinstance(battle_plan.get(key), dict) for _, key in _PLAN_SLOTS)


def _validation_marker(plan: Dict[str, Any]) -> str:
    status = str(plan.get("validation_status") or "").strip().lower()
    return _VALIDATION_MARKERS.get(status, "—")


def _iter_plans(battle_plan: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    if not isinstance(battle_plan, dict):
        return []
    return [
        (label, battle_plan.get(key))
        for label, key in _PLAN_SLOTS
        if isinstance(battle_plan.get(key), dict) and battle_plan.get(key)
    ]


def render_directional_plan_overview(battle_plan: Dict[str, Any]) -> List[str]:
    """Render the long/short/intraday overview table plus validation notes."""
    plans = _iter_plans(battle_plan)
    if not plans:
        return []

    lines = [
        "**🧭 BTC 双向计划概览**",
        "",
        "| 计划 | 方向 | 入场 | 止损 | 目标 | 触发条件 | 执行校验 |",
        "|------|------|------|------|------|----------|----------|",
    ]
    for label, plan in plans:
        entry = clean_plan_value(plan.get("entry_price") or plan.get("entry_zone"))
        lines.append(
            "| {label} | {direction} | {entry} | {stop} | {target} | {trigger} | {validation} |".format(
                label=label,
                direction=clean_plan_value(plan.get("direction")),
                entry=entry,
                stop=clean_plan_value(plan.get("stop_loss")),
                target=clean_plan_value(plan.get("take_profit")),
                trigger=clean_plan_value(plan.get("trigger_condition")),
                validation=_validation_marker(plan),
            )
        )
    lines.append("")

    for label, plan in plans:
        status = str(plan.get("validation_status") or "").strip().lower()
        note = clean_plan_value(plan.get("validation_note"))
        if status in {"failed", "skipped"} and note != "N/A":
            lines.append(f"- {label}：{note}")
    if len(lines) > 5 and lines[-1].startswith("- "):
        lines.append("")
    return lines


def render_intraday_plan_detail(intraday: Dict[str, Any]) -> List[str]:
    """Render the detailed hourly intraday plan block (constraints & reason)."""
    if not isinstance(intraday, dict) or not intraday:
        return []
    return [
        "**⏱️ 小时线日内计划**",
        "",
        "| 项目 | 计划 |",
        "|------|------|",
        f"| 方向 | {clean_plan_value(intraday.get('direction'))} |",
        f"| 入场 | {clean_plan_value(intraday.get('entry_price'))} |",
        f"| 止损 | {clean_plan_value(intraday.get('stop_loss'))} |",
        f"| 目标 | {clean_plan_value(intraday.get('take_profit'))} |",
        f"| 触发 | {clean_plan_value(intraday.get('trigger_condition'))} |",
        f"| 日线约束 | {clean_plan_value(intraday.get('daily_constraint'))} |",
        f"| 执行校验 | {_validation_marker(intraday)} |",
        f"| 依据 | {clean_plan_value(intraday.get('reason'))} |",
        "",
    ]


def render_execution_ladders(battle_plan: Dict[str, Any]) -> List[str]:
    """Render optional staged BTC execution ladders (trial/add/invalidation)."""
    ladders = [
        (label, plan.get("execution_ladder"))
        for label, plan in _iter_plans(battle_plan)
        if isinstance(plan.get("execution_ladder"), dict)
    ]
    if not ladders:
        return []

    lines = [
        "**🪜 BTC 分步执行**",
        "",
        "| 计划 | 阶段 | 价格/区间 | 触发条件 | 仓位/动作 |",
        "|------|------|-----------|----------|-----------|",
    ]
    for plan_label, ladder in ladders:
        scenario = clean_plan_value(ladder.get("scenario"))
        current_action = clean_plan_value(ladder.get("current_action"))
        trial = ladder.get("trial_entry") if isinstance(ladder.get("trial_entry"), dict) else {}
        confirm = ladder.get("confirmation_add") if isinstance(ladder.get("confirmation_add"), dict) else {}
        invalidation = ladder.get("invalidation") if isinstance(ladder.get("invalidation"), dict) else {}

        trial_price = trial.get("entry_price") or trial.get("entry_zone")
        trial_action = trial.get("position_hint") or ("未启用" if trial.get("enabled") is False else "试仓")
        confirm_action = confirm.get("position_hint") or ("未启用" if confirm.get("enabled") is False else "确认后加仓")
        invalidation_action = invalidation.get("action") or "撤销/退出"

        lines.extend([
            f"| {plan_label}（{scenario}；当前={current_action}） | 试仓 | {clean_plan_value(trial_price)} | {clean_plan_value(trial.get('trigger_condition'))} | {clean_plan_value(trial_action)} |",
            f"| {plan_label} | 确认加仓 | {clean_plan_value(confirm.get('entry_price'))} | {clean_plan_value(confirm.get('trigger_condition'))} | {clean_plan_value(confirm_action)} |",
            f"| {plan_label} | 失效 | {clean_plan_value(invalidation.get('price'))} | {clean_plan_value(invalidation.get('condition'))} | {clean_plan_value(invalidation_action)} |",
        ])
    lines.append("")
    return lines
