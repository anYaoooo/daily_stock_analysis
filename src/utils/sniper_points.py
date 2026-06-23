# -*- coding: utf-8 -*-
"""Helpers for parsing report sniper-point price values."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Dict, Optional


SNIPER_KEYS = ("ideal_buy", "secondary_buy", "stop_loss", "take_profit")
STRATEGY_PLAN_KEYS = (
    "entry_price",
    "stop_loss",
    "take_profit",
    "trigger_condition",
    "invalidation",
    "reason",
)


def parse_sniper_value(value: Any) -> Optional[float]:
    """Parse a sniper point value from report text into a positive price."""

    if value is None:
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if parsed > 0 else None

    text = str(value).replace(",", "").replace("，", "").strip()
    if not text or text in {"-", "—", "N/A"}:
        return None

    try:
        parsed = float(text)
        return parsed if parsed > 0 else None
    except ValueError:
        pass

    colon_pos = max(text.rfind("："), text.rfind(":"))
    yuan_pos = text.find("元", colon_pos + 1 if colon_pos != -1 else 0)
    if yuan_pos != -1:
        segment_start = colon_pos + 1 if colon_pos != -1 else 0
        segment = text[segment_start:yuan_pos]
        valid_numbers = []
        for match in re.finditer(r"-?\d+(?:\.\d+)?", segment):
            start_idx = match.start()
            if start_idx >= 2 and segment[start_idx - 2:start_idx].upper() == "MA":
                continue
            valid_numbers.append(match.group())
        if valid_numbers:
            try:
                parsed = abs(float(valid_numbers[-1]))
                return parsed if parsed > 0 else None
            except ValueError:
                pass

    paren_pos = len(text)
    for paren_char in ("(", "（"):
        pos = text.find(paren_char)
        if pos != -1:
            paren_pos = min(paren_pos, pos)
    search_text = text[:paren_pos].strip() or text

    valid_numbers = []
    for match in re.finditer(r"\d+(?:\.\d+)?", search_text):
        start_idx = match.start()
        if start_idx >= 2 and search_text[start_idx - 2:start_idx].upper() == "MA":
            continue
        valid_numbers.append(match.group())
    if valid_numbers:
        try:
            parsed = float(valid_numbers[-1])
            return parsed if parsed > 0 else None
        except ValueError:
            pass
    return None


def extract_sniper_points(result: Any) -> Dict[str, Optional[float]]:
    """Extract normalized sniper-point prices from a completed analysis result."""

    raw_points: Mapping[str, Any] = {}

    if hasattr(result, "get_sniper_points"):
        candidate = result.get_sniper_points() or {}
        if isinstance(candidate, Mapping):
            raw_points = candidate

    if not _has_any_sniper_value(raw_points):
        dashboard = getattr(result, "dashboard", None)
        if isinstance(dashboard, Mapping):
            raw_points = find_sniper_points(dashboard) or raw_points

    if not _has_any_sniper_value(raw_points):
        raw_response = getattr(result, "raw_response", None)
        if isinstance(raw_response, Mapping):
            raw_points = find_sniper_points(raw_response) or raw_points

    return {key: parse_sniper_value(raw_points.get(key)) for key in SNIPER_KEYS}


def extract_directional_strategy_plans(source: Any) -> Dict[str, Optional[Dict[str, str]]]:
    """Extract optional long/short plan blocks from a report dashboard."""

    dashboard: Mapping[str, Any] = {}
    if hasattr(source, "dashboard"):
        candidate = getattr(source, "dashboard", None)
        if isinstance(candidate, Mapping):
            dashboard = candidate
    elif isinstance(source, Mapping):
        if "long_plan" in source or "short_plan" in source or "longPlan" in source or "shortPlan" in source:
            return {
                "long_plan": _normalize_strategy_plan(
                    source.get("long_plan") or source.get("longPlan")
                ),
                "short_plan": _normalize_strategy_plan(
                    source.get("short_plan") or source.get("shortPlan")
                ),
            }
        candidate = source.get("dashboard")
        if isinstance(candidate, Mapping):
            dashboard = candidate
        else:
            dashboard = source

    battle_plan = dashboard.get("battle_plan") if isinstance(dashboard, Mapping) else None
    if not isinstance(battle_plan, Mapping):
        return {"long_plan": None, "short_plan": None}

    return {
        "long_plan": _normalize_strategy_plan(
            battle_plan.get("long_plan") or battle_plan.get("longPlan")
        ),
        "short_plan": _normalize_strategy_plan(
            battle_plan.get("short_plan") or battle_plan.get("shortPlan")
        ),
    }


def _normalize_strategy_plan(value: Any) -> Optional[Dict[str, str]]:
    if not isinstance(value, Mapping):
        return None

    normalized: Dict[str, str] = {}
    for key in STRATEGY_PLAN_KEYS:
        raw_value = value.get(key)
        if raw_value is None and "_" in key:
            camel_key = key.split("_", 1)[0] + "".join(
                part.capitalize() for part in key.split("_")[1:]
            )
            raw_value = value.get(camel_key)
        if raw_value is None:
            continue
        text = str(raw_value).strip()
        if text:
            normalized[key] = text

    return normalized or None


def _has_any_sniper_value(points: Mapping[str, Any]) -> bool:
    return any(points.get(key) not in (None, "") for key in SNIPER_KEYS)


def find_sniper_points(data: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    if not isinstance(data, Mapping):
        return None

    if any(key in data for key in SNIPER_KEYS):
        return data

    sniper_points = data.get("sniper_points")
    if isinstance(sniper_points, Mapping) and sniper_points:
        return sniper_points

    battle_plan = data.get("battle_plan")
    if isinstance(battle_plan, Mapping):
        sniper_points = battle_plan.get("sniper_points")
        if isinstance(sniper_points, Mapping) and sniper_points:
            return sniper_points

    inner_dashboard = data.get("dashboard")
    if isinstance(inner_dashboard, Mapping):
        found = find_sniper_points(inner_dashboard)
        if found:
            return found

    return None
