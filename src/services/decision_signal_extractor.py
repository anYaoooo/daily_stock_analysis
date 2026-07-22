# -*- coding: utf-8 -*-
"""Extract DecisionSignal payloads from completed analysis reports."""

from __future__ import annotations

import logging
import math
import re
from typing import Any, Dict, Mapping, Optional

from data_provider.base import normalize_stock_code

from src.analyzer import AnalysisResult
from src.core.trading_calendar import get_market_for_stock
from src.schemas.decision_action import build_action_fields
from src.services.decision_signal_service import DecisionSignalService
from src.utils.sniper_points import (
    extract_directional_strategy_plans,
    extract_sniper_points,
    parse_sniper_value,
)


logger = logging.getLogger(__name__)

_CONFIDENCE_MAP = {
    "高": 0.8,
    "high": 0.8,
    "中": 0.6,
    "medium": 0.6,
    "mid": 0.6,
    "低": 0.4,
    "low": 0.4,
}


def build_decision_signal_payload_from_report(
    result: AnalysisResult,
    *,
    context_snapshot: Dict[str, Any] | None = None,
    source_report_id: int | None = None,
    trace_id: str,
    query_source: str,
    report_type: str,
) -> Dict[str, Any] | None:
    """Build a DecisionSignal payload from a completed stock analysis report."""

    if result is None or not getattr(result, "success", True):
        return None

    action_fields = build_action_fields(
        operation_advice=getattr(result, "operation_advice", None),
        explicit_action=getattr(result, "action", None),
        report_type=report_type,
        report_language=getattr(result, "report_language", None),
    )
    action = action_fields.get("action")
    if not action:
        return None

    raw_code = str(getattr(result, "code", "") or "").strip()
    market = get_market_for_stock(normalize_stock_code(raw_code))
    if not market:
        logger.warning("Skip decision signal extraction: unrecognized market stock_code=%s", raw_code)
        return None

    dashboard = _as_mapping(getattr(result, "dashboard", None))
    sniper_points = extract_sniper_points(result)
    strategy_plan = _select_primary_strategy_plan(
        result,
        context_snapshot=context_snapshot,
        default_action=action,
    )
    plan_payload = _as_mapping(strategy_plan.get("plan"))
    plan_key = strategy_plan.get("key")
    action = _action_from_strategy_plan(plan_key, plan_payload, action)
    entry_low, entry_high = _entry_range(
        *_entry_values_from_strategy_plan(plan_payload, sniper_points)
    )

    metadata = {
        "report_type": report_type,
        "decision_type": getattr(result, "decision_type", None),
        "report_confidence_level": getattr(result, "confidence_level", None),
        "report_language": getattr(result, "report_language", None),
    }
    market_phase_summary = _extract_market_phase_summary(context_snapshot, result)
    if market_phase_summary:
        metadata["market_phase_summary"] = market_phase_summary
    if plan_payload:
        metadata["strategy_plan"] = _strategy_plan_metadata(plan_key, plan_payload)

    payload: Dict[str, Any] = {
        "stock_code": raw_code,
        "stock_name": getattr(result, "name", None),
        "market": market,
        "source_type": "analysis",
        "source_report_id": source_report_id,
        "trace_id": trace_id,
        "market_phase": _extract_market_phase(context_snapshot, result),
        "trigger_source": str(query_source or "").strip() or "system",
        "action": action,
        "action_label": action_fields.get("action_label"),
        "confidence": _confidence_from_level(getattr(result, "confidence_level", None)),
        "score": _score_from_result(getattr(result, "sentiment_score", None)),
        "horizon": _horizon_from_strategy_plan(plan_key, context_snapshot),
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop_loss": _first_price(plan_payload.get("stop_loss"), sniper_points.get("stop_loss")),
        "target_price": _first_price(plan_payload.get("take_profit"), sniper_points.get("take_profit")),
        "invalidation": _strategy_invalidation(plan_payload),
        "reason": _first_text(
            plan_payload.get("reason"),
            plan_payload.get("no_trade_reason"),
            getattr(result, "analysis_summary", None),
            getattr(result, "buy_reason", None),
            getattr(result, "key_points", None),
        ),
        "risk_summary": _risk_summary(result, dashboard),
        "catalyst_summary": _catalyst_summary(dashboard),
        "watch_conditions": _watch_conditions(dashboard, plan_payload),
        "evidence": _evidence(result, sniper_points, strategy_plan),
        "data_quality_summary": _extract_data_quality(context_snapshot, result),
        "metadata": metadata,
        "report_language": getattr(result, "report_language", None),
    }
    return {key: value for key, value in payload.items() if value not in (None, "", [], {})}


def extract_and_persist_from_analysis_result(
    result: AnalysisResult,
    *,
    context_snapshot: Dict[str, Any] | None = None,
    source_report_id: int | None = None,
    trace_id: str,
    query_source: str,
    report_type: str,
    service: Optional[DecisionSignalService] = None,
) -> Dict[str, Any] | None:
    """Best-effort extract and persist a DecisionSignal from an analysis result."""

    try:
        payload = build_decision_signal_payload_from_report(
            result,
            context_snapshot=context_snapshot,
            source_report_id=source_report_id,
            trace_id=trace_id,
            query_source=query_source,
            report_type=report_type,
        )
        if payload is None:
            return None
        writer = service or DecisionSignalService()
        payload = _apply_crypto_plan_freeze(payload, writer)
        return writer.create_signal(payload)
    except Exception as exc:
        logger.warning(
            "Decision signal extraction failed: query_id=%s stock_code=%s error=%s",
            trace_id,
            getattr(result, "code", None),
            exc,
            exc_info=True,
        )
        return None


def _as_mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _first_text(*values: Any) -> Optional[str]:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _score_from_result(value: Any) -> Optional[int]:
    try:
        score = int(float(value))
    except (TypeError, ValueError):
        return None
    return score if 0 <= score <= 100 else None


def _confidence_from_level(value: Any) -> Optional[float]:
    key = str(value or "").strip().lower()
    return _CONFIDENCE_MAP.get(key)


def _entry_range(ideal_buy: Optional[float], secondary_buy: Optional[float]) -> tuple[Optional[float], Optional[float]]:
    """Return numeric entry bounds while preserving single-value source semantics."""

    low = ideal_buy if ideal_buy is not None and math.isfinite(ideal_buy) and ideal_buy > 0 else None
    high = secondary_buy if secondary_buy is not None and math.isfinite(secondary_buy) and secondary_buy > 0 else None
    if low is not None and high is not None and low > high:
        return high, low
    return low, high


def _select_primary_strategy_plan(
    result: AnalysisResult,
    *,
    context_snapshot: Optional[Mapping[str, Any]],
    default_action: str,
) -> Dict[str, Any]:
    plans = extract_directional_strategy_plans(result)
    if not any(plans.values()):
        raw_response = getattr(result, "raw_response", None)
        if isinstance(raw_response, Mapping):
            plans = extract_directional_strategy_plans(raw_response)

    analysis_mode = _analysis_mode_from_snapshot(context_snapshot)
    intraday = _as_mapping(plans.get("intraday_plan"))
    if intraday and (analysis_mode == "hourly" or _is_intraday_plan_enabled(intraday)):
        return {"key": "intraday_plan", "plan": intraday}

    if default_action in {"buy", "add"} and plans.get("long_plan"):
        return {"key": "long_plan", "plan": plans["long_plan"]}
    if default_action in {"sell", "reduce"} and plans.get("short_plan"):
        return {"key": "short_plan", "plan": plans["short_plan"]}

    if intraday:
        return {"key": "intraday_plan", "plan": intraday}
    if plans.get("long_plan"):
        return {"key": "long_plan", "plan": plans["long_plan"]}
    if plans.get("short_plan"):
        return {"key": "short_plan", "plan": plans["short_plan"]}
    return {"key": None, "plan": None}


def _analysis_mode_from_snapshot(context_snapshot: Optional[Mapping[str, Any]]) -> str:
    snapshot = _as_mapping(context_snapshot)
    mode = snapshot.get("analysis_mode")
    if not mode:
        enhanced_context = _as_mapping(snapshot.get("enhanced_context"))
        mode = enhanced_context.get("analysis_mode")
    normalized = str(mode or "daily").strip().lower()
    return normalized if normalized in {"daily", "hourly"} else "daily"


def _is_intraday_plan_enabled(plan: Mapping[str, Any]) -> bool:
    enabled = plan.get("enabled")
    if isinstance(enabled, bool):
        return enabled
    enabled_text = str(enabled or "").strip().lower()
    if enabled_text in {"true", "1", "yes", "y", "enabled"}:
        return True
    direction = str(plan.get("direction") or "").strip().lower()
    return direction in {"long", "short"} and not _first_text(plan.get("no_trade_reason"))


def _action_from_strategy_plan(
    plan_key: Any,
    plan: Mapping[str, Any],
    default_action: str,
) -> str:
    direction = str(plan.get("direction") or "").strip().lower()
    is_waiting = direction in {"none", "wait", "watch"} or (
        plan_key == "intraday_plan" and not _is_intraday_plan_enabled(plan)
    )
    if is_waiting:
        return default_action if default_action in {"hold", "watch", "avoid", "alert"} else "watch"
    if direction == "long":
        return "buy"
    if direction == "short":
        return "sell"
    return default_action


def _entry_values_from_strategy_plan(
    plan: Mapping[str, Any],
    sniper_points: Mapping[str, Any],
) -> tuple[Optional[float], Optional[float]]:
    direction = str(plan.get("direction") or "").strip().lower()
    if plan and (direction not in {"long", "short"} or not _is_intraday_plan_enabled(plan)):
        return None, None
    zone_values = _positive_numbers(plan.get("entry_zone"))
    if len(zone_values) >= 2:
        return min(zone_values[0], zone_values[1]), max(zone_values[0], zone_values[1])

    entry_price = parse_sniper_value(plan.get("entry_price"))
    if len(zone_values) == 1:
        if entry_price is not None:
            return min(zone_values[0], entry_price), max(zone_values[0], entry_price)
        return zone_values[0], None
    if entry_price is not None:
        return entry_price, None
    return sniper_points.get("ideal_buy"), sniper_points.get("secondary_buy")


def _apply_crypto_plan_freeze(
    payload: Dict[str, Any],
    service: DecisionSignalService,
) -> Dict[str, Any]:
    """Keep an actionable BTC plan fixed until it expires or reverses direction."""

    if payload.get("market") != "crypto":
        return payload
    stock_code = str(payload.get("stock_code") or "").strip()
    horizon = payload.get("horizon")
    if not stock_code or not horizon:
        return payload

    active = service.get_latest_active(
        stock_code=stock_code,
        market="crypto",
        limit=20,
    ).get("items", [])
    same_horizon = [item for item in active if item.get("horizon") == horizon]
    new_action = str(payload.get("action") or "").strip().lower()
    new_direction = _signal_action_direction(new_action)

    if new_direction is not None:
        for item in same_horizon:
            if _signal_action_direction(str(item.get("action") or "")) is None:
                service.update_status(int(item["id"]), status="archived")

    actionable = [
        item
        for item in same_horizon
        if _signal_action_direction(str(item.get("action") or "")) is not None
    ]
    if not actionable:
        return payload

    frozen = min(
        actionable,
        key=lambda item: (str(item.get("created_at") or ""), int(item.get("id") or 0)),
    )
    frozen_direction = _signal_action_direction(str(frozen.get("action") or ""))
    if new_direction is not None and new_direction != frozen_direction:
        return payload

    metadata = dict(payload.get("metadata") or {})
    metadata["plan_lifecycle"] = {
        "state": "superseded_candidate",
        "frozen_by_signal_id": frozen.get("id"),
        "frozen_until": frozen.get("expires_at"),
        "reason": "active_plan_still_valid",
    }
    payload = dict(payload)
    payload["metadata"] = metadata
    payload["status"] = "archived"
    return payload


def _signal_action_direction(action: str) -> Optional[str]:
    normalized = str(action or "").strip().lower()
    if normalized in {"buy", "add"}:
        return "long"
    if normalized in {"sell", "reduce"}:
        return "short"
    return None


def _positive_numbers(value: Any) -> list[float]:
    if value in (None, ""):
        return []
    if isinstance(value, (int, float)):
        number = float(value)
        return [number] if math.isfinite(number) and number > 0 else []
    text = str(value).replace(",", "").replace("，", "").strip()
    numbers: list[float] = []
    for match in re.finditer(r"\d+(?:\.\d+)?", text):
        start_idx = match.start()
        if start_idx >= 2 and text[start_idx - 2:start_idx].upper() == "MA":
            continue
        try:
            number = float(match.group())
        except ValueError:
            continue
        if math.isfinite(number) and number > 0:
            numbers.append(number)
    return numbers


def _first_price(*values: Any) -> Optional[float]:
    for value in values:
        if isinstance(value, (int, float)):
            number = float(value)
            if math.isfinite(number) and number > 0:
                return number
            continue
        parsed = parse_sniper_value(value)
        if parsed is not None:
            return parsed
    return None


def _strategy_invalidation(plan: Mapping[str, Any]) -> Optional[str]:
    return _first_text(plan.get("invalid_condition"), plan.get("invalidation"))


def _horizon_from_strategy_plan(
    plan_key: Any,
    context_snapshot: Optional[Mapping[str, Any]],
) -> Optional[str]:
    if plan_key == "intraday_plan":
        return "intraday"
    if _analysis_mode_from_snapshot(context_snapshot) == "hourly":
        return "intraday"
    if plan_key in {"long_plan", "short_plan"}:
        return "3d"
    return None


def _extract_market_phase(context_snapshot: Optional[Mapping[str, Any]], result: AnalysisResult) -> Optional[str]:
    snapshot_phase = _as_mapping(_as_mapping(context_snapshot).get("market_phase_summary")).get("phase")
    if snapshot_phase:
        return str(snapshot_phase)
    result_phase = _as_mapping(getattr(result, "market_phase_summary", None)).get("phase")
    return str(result_phase) if result_phase else None


def _extract_market_phase_summary(
    context_snapshot: Optional[Mapping[str, Any]],
    result: AnalysisResult,
) -> Optional[Dict[str, Any]]:
    raw_summary = _as_mapping(_as_mapping(context_snapshot).get("market_phase_summary"))
    if not raw_summary:
        raw_summary = _as_mapping(getattr(result, "market_phase_summary", None))
    allowed_fields = ("phase", "session_date", "minutes_to_open", "minutes_to_close")
    summary = {
        field_name: raw_summary.get(field_name)
        for field_name in allowed_fields
        if raw_summary.get(field_name) not in (None, "")
    }
    return summary or None


def _extract_data_quality(context_snapshot: Optional[Mapping[str, Any]], result: AnalysisResult) -> Optional[Any]:
    snapshot_quality = _as_mapping(
        _as_mapping(context_snapshot).get("analysis_context_pack_overview")
    ).get("data_quality")
    if snapshot_quality:
        return snapshot_quality
    return _as_mapping(getattr(result, "analysis_context_pack_overview", None)).get("data_quality")


def _risk_summary(result: AnalysisResult, dashboard: Mapping[str, Any]) -> Optional[Any]:
    risks = []
    risk_warning = getattr(result, "risk_warning", None)
    if risk_warning:
        risks.append(str(risk_warning))
    intelligence = _as_mapping(dashboard.get("intelligence"))
    risk_alerts = intelligence.get("risk_alerts")
    if isinstance(risk_alerts, list):
        risks.extend(str(item) for item in risk_alerts if str(item or "").strip())
    return risks[:5] or None


def _catalyst_summary(dashboard: Mapping[str, Any]) -> Optional[Any]:
    catalysts = _as_mapping(dashboard.get("intelligence")).get("positive_catalysts")
    if not isinstance(catalysts, list):
        return None
    out = [str(item) for item in catalysts if str(item or "").strip()]
    return out[:5] or None


def _watch_conditions(dashboard: Mapping[str, Any], plan: Optional[Mapping[str, Any]] = None) -> Optional[Any]:
    conditions = []
    plan_map = _as_mapping(plan)
    for value in (
        plan_map.get("trigger_condition"),
        plan_map.get("daily_constraint"),
        plan_map.get("no_trade_reason"),
    ):
        text = str(value or "").strip()
        if text and text not in conditions:
            conditions.append(text)

    phase_decision = _as_mapping(dashboard.get("phase_decision"))
    watch_conditions = phase_decision.get("watch_conditions")
    if isinstance(watch_conditions, list) and watch_conditions:
        for item in watch_conditions:
            text = str(item or "").strip()
            if text and text not in conditions:
                conditions.append(text)
        return conditions or None

    battle_plan = _as_mapping(dashboard.get("battle_plan"))
    checklist = battle_plan.get("action_checklist")
    if isinstance(checklist, list) and checklist:
        for item in checklist:
            text = str(item or "").strip()
            if text and text not in conditions:
                conditions.append(text)
    return conditions or None


def _strategy_plan_metadata(plan_key: Any, plan: Mapping[str, Any]) -> Dict[str, Any]:
    execution_contract = _as_mapping(plan.get("execution_contract"))
    contract_entry = _as_mapping(execution_contract.get("entry"))
    metadata = {
        "source": plan_key,
        "plan_type": plan.get("plan_type"),
        "setup_type": contract_entry.get("setup_type") or plan.get("setup_type"),
        "direction": plan.get("direction"),
        "timeframe": plan.get("timeframe"),
        "analysis_timeframe": plan.get("analysis_timeframe"),
        "trigger_condition": plan.get("trigger_condition"),
        "invalidation": _strategy_invalidation(plan),
        "risk_reward": plan.get("risk_reward"),
        "position_hint": plan.get("position_hint"),
        "confidence": plan.get("confidence"),
        "no_trade_reason": plan.get("no_trade_reason"),
        "daily_constraint": plan.get("daily_constraint"),
    }
    return {key: value for key, value in metadata.items() if value not in (None, "", [], {})}


def _evidence(
    result: AnalysisResult,
    sniper_points: Mapping[str, Any],
    strategy_plan: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    evidence = {
        "operation_advice": getattr(result, "operation_advice", None),
        "decision_type": getattr(result, "decision_type", None),
        "trend_prediction": getattr(result, "trend_prediction", None),
        "confidence_level": getattr(result, "confidence_level", None),
        "current_price": getattr(result, "current_price", None),
        "change_pct": getattr(result, "change_pct", None),
        "sniper_points": dict(sniper_points),
    }
    strategy = _as_mapping(strategy_plan)
    plan = _as_mapping(strategy.get("plan"))
    if plan:
        evidence["strategy_plan"] = _strategy_plan_metadata(strategy.get("key"), plan)
    return {key: value for key, value in evidence.items() if value not in (None, "", [], {})}
