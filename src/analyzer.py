# -*- coding: utf-8 -*-
"""
===================================
A股自选股智能分析系统 - AI分析层
===================================

职责：
1. 封装 LLM 调用逻辑（通过 LiteLLM 统一调用 Gemini/Anthropic/OpenAI 等）
2. 结合技术面和消息面生成分析报告
3. 解析 LLM 响应为结构化 AnalysisResult
"""

import copy
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any, List, Tuple, Callable

import litellm
from json_repair import repair_json
from litellm import Router

from data_provider.base import normalize_stock_code
from src.agent.llm_adapter import (
    get_thinking_extra_body,
    resolve_fallback_litellm_wire_models,
    register_fallback_model_pricing,
)
from src.agent.provider_trace import resolved_model_provider_identity
from src.agent.skills.defaults import CORE_TRADING_SKILL_POLICY_ZH
from src.config import (
    Config,
    extra_litellm_params,
    get_api_keys_for_model,
    get_config,
    get_configured_llm_models,
    normalize_litellm_temperature,
    resolve_litellm_wire_model,
    resolve_news_window_days,
)
from src.llm.generation_params import apply_litellm_generation_params
from src.llm.errors import call_litellm_with_param_recovery
from src.llm.usage import (
    attach_message_hmacs,
    extract_usage_payload,
    normalize_litellm_usage,
)
from src.storage import persist_llm_usage
from src.data.stock_mapping import STOCK_NAME_MAP
from src.report_language import (
    get_signal_level,
    get_no_data_text,
    get_placeholder_text,
    get_unknown_text,
    get_chip_unavailable_text,
    infer_decision_type_from_advice,
    is_chip_placeholder_value,
    localize_chip_health,
    localize_confidence_level,
    normalize_report_language,
)
from src.schemas.decision_action import build_action_fields
from src.schemas.report_schema import AnalysisReportSchema
from src.schemas.crypto_instrument import resolve_crypto_instrument
from src.core.crypto_backtest_engine import CryptoBacktestEngine, CryptoPlan, CryptoPlanBacktestConfig
from src.core.trading_calendar import get_market_for_stock
from src.market_context import get_market_role, get_market_guidelines
from src.services.daily_market_context import format_daily_market_context_prompt_section
from src.market_phase_prompt import format_market_phase_prompt_section
from src.utils.timeframe import analysis_timeframe_label, normalize_analysis_mode
from src.utils.sniper_points import parse_sniper_value

logger = logging.getLogger(__name__)


def _normalize_risk_warning_values(value: Any) -> List[str]:
    """Normalize arbitrary risk_warning values into a flat list of text alerts."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple, set)):
        normalized: List[str] = []
        for item in value:
            normalized.extend(_normalize_risk_warning_values(item))
        return normalized
    if isinstance(value, dict):
        if not value:
            return []
        try:
            dumped = json.dumps(value, ensure_ascii=False)
            text = dumped.strip()
        except (TypeError, ValueError):
            text = str(value).strip()
        return [text] if text else []
    text = str(value).strip()
    return [text] if text else []


def _today_has_realtime_overlay(today: Any) -> bool:
    if not isinstance(today, dict):
        return False
    data_source = today.get("data_source") or today.get("dataSource")
    if isinstance(data_source, str) and data_source.startswith("realtime:"):
        return True
    if today.get("is_partial_bar") is True or today.get("isPartialBar") is True:
        return True
    if today.get("is_estimated") is True or today.get("isEstimated") is True:
        return True
    return bool(today.get("estimated_fields") or today.get("estimatedFields"))


def _is_crypto_analysis_context(context: Dict[str, Any]) -> bool:
    """Return whether an analysis context represents a crypto instrument."""
    code = str(context.get("code") or context.get("stock_code") or "")
    return (
        str(context.get("market") or "").strip().lower() == "crypto"
        or get_market_for_stock(normalize_stock_code(code)) == "crypto"
        or isinstance(context.get("crypto_technical"), dict)
    )


def _today_looks_complete_daily_bar(
    context: Dict[str, Any],
    phase_context: Dict[str, Any],
) -> bool:
    today = context.get("today")
    if (
        not isinstance(today, dict)
        or today.get("close") in (None, "")
        or _today_has_realtime_overlay(today)
    ):
        return False

    effective_date = phase_context.get("effective_daily_bar_date")
    today_date = today.get("date") or today.get("trade_date") or context.get("date")
    if effective_date and today_date and str(today_date) != str(effective_date):
        return False
    return True


def _phase_aware_quote_labels(context: Dict[str, Any]) -> Tuple[str, str]:
    """Choose Chinese quote-table labels that do not conflict with phase context."""
    phase_context = context.get("market_phase_context")
    if not isinstance(phase_context, dict):
        return "今日行情", "收盘价"

    phase = str(phase_context.get("phase") or "").strip()
    if phase in {"premarket", "non_trading"}:
        today = context.get("today")
        if _today_looks_complete_daily_bar(context, phase_context):
            return "上一完整交易日行情", "上一完整交易日收盘价"
        if _today_has_realtime_overlay(today):
            return "最新行情", "实时估算价"
        if isinstance(today, dict) and today.get("close") not in (None, ""):
            return "最新行情", "最新价"
        return "今日行情", "收盘价"

    if (
        phase in {"intraday", "lunch_break", "closing_auction"}
        and phase_context.get("is_partial_bar") is True
    ):
        return "最新行情", "盘中估算价"

    return "今日行情", "收盘价"


def _should_hide_regular_session_ohlc(context: Dict[str, Any]) -> bool:
    phase_context = context.get("market_phase_context")
    if not isinstance(phase_context, dict):
        return False

    phase = str(phase_context.get("phase") or "").strip()
    return phase in {"premarket", "non_trading"} and not _today_looks_complete_daily_bar(
        context,
        phase_context,
    )


class _LiteLLMStreamError(RuntimeError):
    """Internal error wrapper that records whether any text was streamed."""

    def __init__(self, message: str, *, partial_received: bool = False):
        super().__init__(message)
        self.partial_received = partial_received


class _AllModelsFailedError(Exception):
    """Raised when every model in the fallback chain fails.

    This includes both LLM call errors and JSON parse errors (when a
    ``response_validator`` is provided to :meth:`GeminiAnalyzer._call_litellm`).

    The ``last_response_text`` attribute holds the raw text from the last model
    that *did* return a response (but whose JSON could not be validated), so
    callers can still attempt a best-effort text fallback.

    ``last_model`` and ``last_usage`` record the model name and token usage
    from the last attempt so callers can persist usage even on fallback.
    """

    def __init__(
        self,
        message: str,
        *,
        last_response_text: Optional[str] = None,
        last_model: Optional[str] = None,
        last_usage: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.last_response_text = last_response_text
        self.last_model = last_model
        self.last_usage = last_usage or {}


def check_content_integrity(
    result: "AnalysisResult",
    *,
    require_phase_decision: bool = False,
) -> Tuple[bool, List[str]]:
    """
    Check mandatory fields for report content integrity.
    Returns (pass, missing_fields). Module-level for use by pipeline (agent weak mode).
    """
    missing: List[str] = []

    def _is_blank_text(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        return True

    def _is_invalid_risk_alerts(value: Any) -> bool:
        return not isinstance(value, list)

    def _is_invalid_stop_loss(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, (list, tuple, dict)):
            return True
        if isinstance(value, str):
            return not value.strip()
        return False

    if result.sentiment_score is None:
        missing.append("sentiment_score")
    advice = result.operation_advice
    if not advice or not isinstance(advice, str) or _is_blank_text(advice):
        missing.append("operation_advice")
    summary = result.analysis_summary
    if not summary or not isinstance(summary, str) or _is_blank_text(summary):
        missing.append("analysis_summary")
    dash = result.dashboard if isinstance(result.dashboard, dict) else {}
    core = dash.get("core_conclusion")
    core = core if isinstance(core, dict) else {}
    if _is_blank_text(core.get("one_sentence")):
        missing.append("dashboard.core_conclusion.one_sentence")
    intel = dash.get("intelligence")
    intel = intel if isinstance(intel, dict) else None
    if intel is None or _is_invalid_risk_alerts(intel.get("risk_alerts")):
        missing.append("dashboard.intelligence.risk_alerts")
    if result.decision_type in ("buy", "hold"):
        battle = dash.get("battle_plan")
        battle = battle if isinstance(battle, dict) else {}
        sp = battle.get("sniper_points")
        sp = sp if isinstance(sp, dict) else {}
        stop_loss = sp.get("stop_loss")
        if _is_invalid_stop_loss(stop_loss):
            missing.append("dashboard.battle_plan.sniper_points.stop_loss")
    if require_phase_decision:
        phase_decision = dash.get("phase_decision")
        phase_decision = phase_decision if isinstance(phase_decision, dict) else {}
        if not isinstance(phase_decision.get("phase_context"), dict):
            missing.append("dashboard.phase_decision.phase_context")
        if _is_blank_text(phase_decision.get("action_window")):
            missing.append("dashboard.phase_decision.action_window")
        if _is_blank_text(phase_decision.get("immediate_action")):
            missing.append("dashboard.phase_decision.immediate_action")
        if not isinstance(phase_decision.get("watch_conditions"), list):
            missing.append("dashboard.phase_decision.watch_conditions")
        if _is_blank_text(phase_decision.get("next_check_time")):
            missing.append("dashboard.phase_decision.next_check_time")
        if _is_blank_text(phase_decision.get("confidence_reason")):
            missing.append("dashboard.phase_decision.confidence_reason")
        if not isinstance(phase_decision.get("data_limitations"), list):
            missing.append("dashboard.phase_decision.data_limitations")
    return len(missing) == 0, missing


def apply_placeholder_fill(result: "AnalysisResult", missing_fields: List[str]) -> None:
    """Fill missing mandatory fields with placeholders (in-place). Module-level for pipeline."""

    def _is_blank_text(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        return True

    def _is_invalid_risk_alerts(value: Any) -> bool:
        return not isinstance(value, list)

    def _is_invalid_stop_loss(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, (list, tuple, dict)):
            return True
        if isinstance(value, str):
            return not value.strip()
        return False

    report_language = normalize_report_language(getattr(result, "report_language", "zh"))
    placeholder = get_placeholder_text(report_language)
    phase_decision_placeholders = {
        "dashboard.phase_decision.action_window": (
            "Model did not provide a phase action window"
            if report_language == "en"
            else "模型未提供阶段化行动窗口"
        ),
        "dashboard.phase_decision.immediate_action": (
            "Model did not provide a phase-aware immediate action"
            if report_language == "en"
            else "模型未提供阶段化即时动作"
        ),
        "dashboard.phase_decision.next_check_time": (
            "Model did not provide a next check point"
            if report_language == "en"
            else "模型未提供下一次检查点"
        ),
        "dashboard.phase_decision.confidence_reason": (
            "Model did not provide a phase confidence rationale"
            if report_language == "en"
            else "模型未提供阶段化置信度理由"
        ),
    }
    for field in missing_fields:
        if field == "sentiment_score":
            result.sentiment_score = 50
        elif field == "operation_advice":
            if _is_blank_text(result.operation_advice):
                result.operation_advice = placeholder
        elif field == "analysis_summary":
            if _is_blank_text(result.analysis_summary):
                result.analysis_summary = placeholder
        elif field == "dashboard.core_conclusion.one_sentence":
            if not result.dashboard:
                result.dashboard = {}
            core = result.dashboard.get("core_conclusion")
            if not isinstance(core, dict):
                core = {}
                result.dashboard["core_conclusion"] = core
            fallback_sentence = (
                result.analysis_summary
                or result.operation_advice
                or placeholder
            )
            if _is_blank_text(core.get("one_sentence")):
                result.dashboard["core_conclusion"]["one_sentence"] = fallback_sentence
        elif field == "dashboard.intelligence.risk_alerts":
            if not result.dashboard:
                result.dashboard = {}
            intelligence = result.dashboard.get("intelligence")
            if not isinstance(intelligence, dict):
                intelligence = {}
                result.dashboard["intelligence"] = intelligence
            if _is_invalid_risk_alerts(intelligence.get("risk_alerts")):
                risk_warning_values = _normalize_risk_warning_values(result.risk_warning)
                intelligence["risk_alerts"] = risk_warning_values
        elif field == "dashboard.battle_plan.sniper_points.stop_loss":
            if not result.dashboard:
                result.dashboard = {}
            battle_plan = result.dashboard.get("battle_plan")
            if not isinstance(battle_plan, dict):
                battle_plan = {}
                result.dashboard["battle_plan"] = battle_plan
            sniper_points = battle_plan.get("sniper_points")
            if not isinstance(sniper_points, dict):
                sniper_points = {}
                battle_plan["sniper_points"] = sniper_points
            if _is_invalid_stop_loss(sniper_points.get("stop_loss")):
                sniper_points["stop_loss"] = placeholder
        elif field.startswith("dashboard.phase_decision."):
            if not result.dashboard:
                result.dashboard = {}
            phase_decision = result.dashboard.get("phase_decision")
            if not isinstance(phase_decision, dict):
                phase_decision = {}
                result.dashboard["phase_decision"] = phase_decision
            if field == "dashboard.phase_decision.phase_context":
                if not isinstance(phase_decision.get("phase_context"), dict):
                    phase_decision["phase_context"] = {}
            elif field == "dashboard.phase_decision.watch_conditions":
                if not isinstance(phase_decision.get("watch_conditions"), list):
                    phase_decision["watch_conditions"] = []
            elif field == "dashboard.phase_decision.data_limitations":
                if not isinstance(phase_decision.get("data_limitations"), list):
                    phase_decision["data_limitations"] = []
            elif field in phase_decision_placeholders:
                if _is_blank_text(phase_decision.get(field.rsplit(".", 1)[-1])):
                    phase_decision[field.rsplit(".", 1)[-1]] = phase_decision_placeholders[field]


# ---------- chip_structure fallback (Issue #589) ----------

_CHIP_KEYS: tuple = ("profit_ratio", "avg_cost", "concentration", "chip_health")
_BTC_MFE_MIN_SAMPLE_COUNT = 10
_BTC_TRADEABILITY_GUARD_VERSION = "btc-tradeability-v1"


def _is_value_placeholder(v: Any) -> bool:
    """True if value is empty or placeholder (N/A, 数据缺失, etc.)."""
    return is_chip_placeholder_value(v)


_RISK_WARNING_PLACEHOLDER_TEXTS = {
    "",
    "n/a",
    "na",
    "none",
    "null",
    "unknown",
    "tbd",
    "暂无",
    "待补充",
    "数据缺失",
    "未知",
    "无",
}

_STRUCTURAL_RISK_PHRASE_HINTS = (
    "重大利空",
    "重大风险",
    "关键风险",
    "减持",
    "高位减持",
    "退市",
    "退市风险",
    "停牌",
    "重大问询",
    "处罚",
    "限售",
    "违规",
    "违规风险",
    "诉讼",
    "问询",
    "监管",
    "财务",
    "审计",
    "爆雷",
    "暴雷",
    "违约",
    "违约风险",
    "流动性危机",
    "债务",
    "清算",
    "破产",
    "重大变脸",
    "major risk",
    "material adverse",
    "suspension",
    "delisting",
    "regulatory",
    "downgrade",
    "liquidity",
    "default",
)

_CAPITAL_FLOW_UNAVAILABLE_STATUS = {
    "not_supported",
    "not supported",
    "unsupported",
    "unavailable",
    "not_available",
    "not available",
    "none",
    "na",
    "n/a",
    "null",
    "missing",
}


def _is_meaningful_text(value: Any) -> bool:
    text = str(value).strip() if value is not None else ""
    if not text:
        return False
    lowered = text.strip().lower()
    return lowered not in _RISK_WARNING_PLACEHOLDER_TEXTS


def _safe_float(v: Any, default: float = 0.0) -> float:
    """Safely convert to float; return default on failure. Private helper for chip fill."""
    if v is None:
        return default
    if isinstance(v, (int, float)):
        try:
            return default if math.isnan(float(v)) else float(v)
        except (ValueError, TypeError):
            return default
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return default


def _coerce_chip_metric(v: Any) -> Optional[float]:
    """Convert chip metrics while preserving the distinction between missing and zero."""
    if v is None:
        return None
    try:
        numeric = float(v)
    except (TypeError, ValueError):
        try:
            numeric = float(str(v).strip())
        except (TypeError, ValueError):
            return None
    return None if math.isnan(numeric) else numeric


_BULLISH_TREND_HINTS: Tuple[str, ...] = (
    "多头排列",
    "持续上涨",
    "趋势向上",
    "上升趋势",
    "向上发散",
    "bullish",
    "uptrend",
)
_WEAK_BULLISH_TREND_HINTS: Tuple[str, ...] = ("弱势多头",)
_BEARISH_TREND_HINTS: Tuple[str, ...] = (
    "空头排列",
    "持续下跌",
    "趋势向下",
    "下降趋势",
    "向下发散",
    "bearish",
    "downtrend",
)
_WEAK_BEARISH_TREND_HINTS: Tuple[str, ...] = ("弱势空头",)
_NEGATION_TOKENS: Tuple[str, ...] = (
    "不是",
    "并非",
    "并未",
    "没有",
    "尚不",
    "尚未",
    "未",
    "无",
    "不属",
    "非",
    "not ",
    "no ",
)
_NEGATION_BREAK_CHARS: Tuple[str, ...] = (",", ".", ";", ":", "!", "?", "，", "。", "；", "：", "！", "？", "\n")
_NEGATION_LOOKBACK_CHARS = 16
_NEGATION_MAX_GAP_CHARS = 8
_NEGATION_SCOPE_BREAK_TOKENS: Tuple[str, ...] = (
    "而是",
    "但是",
    "但",
    "反而",
    "反倒",
    "转为",
    "转成",
    "改为",
    "改成",
    " but ",
    " instead ",
    " rather ",
)
_SINGLE_CHAR_NEGATION_GAP_PREFIXES: Tuple[str, ...] = (
    "形成",
    "出现",
    "进入",
    "转为",
    "转成",
    "构成",
    "呈现",
    "显示",
    "属于",
    "是",
    "有",
    "能",
    "见",
    "站",
    "守",
    "破",
)


def _normalize_prompt_reason_items(items: Any) -> List[str]:
    """Normalize prompt reason/risk items into a clean string list."""
    if not isinstance(items, list):
        return []
    normalized: List[str] = []
    for item in items:
        text = str(item).strip()
        if text:
            normalized.append(text)
    return normalized


def _contains_trend_hint(text: str, hints: Tuple[str, ...]) -> bool:
    """Return True when text contains a non-negated strong trend hint."""
    lowered = text.strip().lower()

    def _has_negation_scope_break(gap: str) -> bool:
        normalized_gap = gap.lower()
        for token in _NEGATION_SCOPE_BREAK_TOKENS:
            token_index = normalized_gap.find(token)
            if token_index > 0:
                return True
        return False

    def _is_valid_negation_gap(token: str, gap: str) -> bool:
        if not gap:
            return True
        if token not in {"未", "无", "非"}:
            return True
        return any(gap.startswith(prefix) for prefix in _SINGLE_CHAR_NEGATION_GAP_PREFIXES)

    def _is_negated_match(index: int) -> bool:
        prefix = lowered[max(0, index - _NEGATION_LOOKBACK_CHARS):index]
        for token in _NEGATION_TOKENS:
            token_index = prefix.rfind(token)
            if token_index < 0:
                continue
            gap = prefix[token_index + len(token):]
            if any(char in gap for char in _NEGATION_BREAK_CHARS):
                continue
            stripped_gap = gap.strip()
            if len(stripped_gap) > _NEGATION_MAX_GAP_CHARS:
                continue
            if _has_negation_scope_break(stripped_gap):
                continue
            if not _is_valid_negation_gap(token, stripped_gap):
                continue
            return True
        return False

    for hint in hints:
        keyword = hint.lower()
        start = 0
        while True:
            index = lowered.find(keyword, start)
            if index < 0:
                break
            if not _is_negated_match(index):
                return True
            start = index + len(keyword)
    return False


def _infer_trend_direction(trend: Dict[str, Any]) -> str:
    """Infer the final trend direction from trend_status and ma_alignment."""
    combined = " ".join(
        str(trend.get(key, "")).strip()
        for key in ("trend_status", "ma_alignment")
        if str(trend.get(key, "")).strip()
    )
    if not combined:
        return "neutral"
    lowered = combined.lower()
    normalized = lowered.replace(" ", "")
    has_bullish = (
        _contains_trend_hint(combined, _BULLISH_TREND_HINTS + _WEAK_BULLISH_TREND_HINTS)
        or "ma5>ma10>ma20" in normalized
        or (
            "ma5>ma10" in normalized
            and any(pattern in normalized for pattern in ("ma10≤ma20", "ma10<=ma20"))
        )
    )
    has_bearish = (
        _contains_trend_hint(combined, _BEARISH_TREND_HINTS + _WEAK_BEARISH_TREND_HINTS)
        or "ma5<ma10<ma20" in normalized
        or (
            "ma5<ma10" in normalized
            and any(pattern in normalized for pattern in ("ma10≥ma20", "ma10>=ma20"))
        )
    )
    if has_bullish and not has_bearish:
        return "bullish"
    if has_bearish and not has_bullish:
        return "bearish"
    return "neutral"


def _filter_conflicting_trend_items(items: List[str], conflict_hints: Tuple[str, ...]) -> List[str]:
    """Drop reasons that directly conflict with the final trend direction."""
    return [item for item in items if not _contains_trend_hint(item, conflict_hints)]


def _sanitize_trend_analysis_for_prompt(
    trend: Any,
    *,
    volume_change_ratio: Any = None,
) -> Dict[str, Any]:
    """Clean prompt-only trend hints on a derived copy without touching runtime/provider config."""
    trend_dict = dict(trend) if isinstance(trend, dict) else {}
    signal_reasons = _normalize_prompt_reason_items(trend_dict.get("signal_reasons"))
    risk_factors = _normalize_prompt_reason_items(trend_dict.get("risk_factors"))
    prompt_notes: List[str] = []
    trend_direction = _infer_trend_direction(trend_dict)

    if trend_direction == "bearish":
        filtered_signal_reasons = _filter_conflicting_trend_items(
            signal_reasons,
            _BULLISH_TREND_HINTS + _WEAK_BULLISH_TREND_HINTS,
        )
        if len(filtered_signal_reasons) != len(signal_reasons):
            prompt_notes.append("当前技术结构偏空，已剔除与空头主判断直接冲突的看多结构理由。")
        signal_reasons = filtered_signal_reasons
        prompt_notes.append(
            "若新闻、业绩或政策催化偏多，只能表述为“事件先行、技术待确认”或“基本面偏多，但技术面尚未确认”，严禁写成确定性买点。"
        )
    elif trend_direction == "bullish":
        filtered_signal_reasons = _filter_conflicting_trend_items(
            signal_reasons,
            _BEARISH_TREND_HINTS + _WEAK_BEARISH_TREND_HINTS,
        )
        if len(filtered_signal_reasons) != len(signal_reasons):
            prompt_notes.append("当前技术结构偏多，已剔除与多头主判断直接冲突的空头结构理由。")
        signal_reasons = filtered_signal_reasons
        filtered_risk_factors = _filter_conflicting_trend_items(
            risk_factors,
            _BEARISH_TREND_HINTS + _WEAK_BEARISH_TREND_HINTS,
        )
        if len(filtered_risk_factors) != len(risk_factors):
            prompt_notes.append("当前技术结构偏多，已剔除与多头主判断直接冲突的空头结构风险表述。")
        risk_factors = filtered_risk_factors

    parsed_volume_change = _safe_float(volume_change_ratio, default=math.nan)
    if math.isfinite(parsed_volume_change) and parsed_volume_change > 10:
        prompt_notes.append(
            f"成交量较昨日变化约 {parsed_volume_change:.2f} 倍，可能存在异常数据或一次性冲量；量能信号必须降权解读，不能机械视为强确认。"
        )

    trend_dict["signal_reasons"] = signal_reasons
    trend_dict["risk_factors"] = risk_factors
    trend_dict["prompt_consistency_notes"] = prompt_notes
    trend_dict["prompt_trend_direction"] = trend_direction
    return trend_dict


def _derive_chip_health(profit_ratio: float, concentration_90: float, language: str = "zh") -> str:
    """Derive chip_health from profit_ratio and concentration_90."""
    if profit_ratio >= 0.9:
        return localize_chip_health("警惕", language)  # 获利盘极高
    if concentration_90 >= 0.25:
        return localize_chip_health("警惕", language)  # 筹码分散
    if concentration_90 < 0.15 and 0.3 <= profit_ratio < 0.9:
        return localize_chip_health("健康", language)  # 集中且获利比例适中
    return localize_chip_health("一般", language)


def _build_chip_structure_from_data(chip_data: Any, language: str = "zh") -> Dict[str, Any]:
    """Build chip_structure dict from ChipDistribution or dict."""
    if hasattr(chip_data, "profit_ratio"):
        pr = _safe_float(chip_data.profit_ratio)
        ac = chip_data.avg_cost
        c90 = _safe_float(chip_data.concentration_90)
    else:
        d = chip_data if isinstance(chip_data, dict) else {}
        pr = _safe_float(d.get("profit_ratio"))
        ac = d.get("avg_cost")
        c90 = _safe_float(d.get("concentration_90"))
    chip_health = _derive_chip_health(pr, c90, language=language)
    return {
        "profit_ratio": f"{pr:.1%}",
        "avg_cost": ac if (ac is not None and _safe_float(ac) != 0.0) else "N/A",
        "concentration": f"{c90:.2%}",
        "chip_health": chip_health,
    }


def _has_meaningful_chip_data(chip_data: Any) -> bool:
    """Return True when chip data has the core metrics required for reporting."""
    if not chip_data:
        return False
    if hasattr(chip_data, "avg_cost"):
        avg_cost = _coerce_chip_metric(getattr(chip_data, "avg_cost", None))
        concentration_90 = _coerce_chip_metric(getattr(chip_data, "concentration_90", None))
        concentration_70 = _coerce_chip_metric(getattr(chip_data, "concentration_70", None))
    else:
        d = chip_data if isinstance(chip_data, dict) else {}
        avg_cost = _coerce_chip_metric(d.get("avg_cost"))
        concentration_90_value = d.get("concentration_90")
        if concentration_90_value is None:
            concentration_90_value = d.get("concentration")
        concentration_90 = _coerce_chip_metric(concentration_90_value)
        concentration_70 = _coerce_chip_metric(d.get("concentration_70"))
    return (
        avg_cost is not None
        and avg_cost > 0
        and (
            (concentration_90 is not None and concentration_90 >= 0)
            or (concentration_70 is not None and concentration_70 >= 0)
        )
    )


def _mark_chip_structure_unavailable(result: "AnalysisResult", language: str) -> None:
    if not result or not isinstance(result.dashboard, dict):
        return
    data_perspective = result.dashboard.get("data_perspective")
    if not isinstance(data_perspective, dict):
        return
    data_perspective["chip_structure"] = {}
    data_perspective["chip_unavailable_reason"] = get_chip_unavailable_text(language)


def normalize_chip_structure_availability(result: "AnalysisResult", chip_data: Any) -> None:
    """Fill valid chip metrics or collapse placeholder-only chip fields to one fallback line."""
    if not result:
        return
    language = getattr(result, "report_language", "zh")
    if _has_meaningful_chip_data(chip_data):
        fill_chip_structure_if_needed(result, chip_data)
        return
    _mark_chip_structure_unavailable(result, language)


def fill_chip_structure_if_needed(result: "AnalysisResult", chip_data: Any) -> None:
    """When chip_data exists, fill chip_structure placeholder fields from chip_data (in-place)."""
    if not result or not _has_meaningful_chip_data(chip_data):
        return
    try:
        if not result.dashboard:
            result.dashboard = {}
        dash = result.dashboard
        # Use `or {}` rather than setdefault so that an explicit `null` from LLM is also replaced
        dp = dash.get("data_perspective") or {}
        dash["data_perspective"] = dp
        cs = dp.get("chip_structure") or {}
        filled = _build_chip_structure_from_data(
            chip_data,
            language=getattr(result, "report_language", "zh"),
        )
        # Start from a copy of cs to preserve any extra keys the LLM may have added
        merged = dict(cs)
        for k in _CHIP_KEYS:
            if _is_value_placeholder(merged.get(k)):
                merged[k] = filled[k]
        if merged != cs:
            dp["chip_structure"] = merged
            logger.info("[chip_structure] Filled placeholder chip fields from data source (Issue #589)")
    except Exception as e:
        logger.warning("[chip_structure] Fill failed, skipping: %s", e)


_PRICE_POS_KEYS = ("ma5", "ma10", "ma20", "bias_ma5", "bias_status", "current_price", "support_level", "resistance_level")


def fill_price_position_if_needed(
    result: "AnalysisResult",
    trend_result: Any = None,
    realtime_quote: Any = None,
) -> None:
    """Fill missing price_position fields from trend_result / realtime data (in-place)."""
    if not result:
        return
    try:
        if not result.dashboard:
            result.dashboard = {}
        dash = result.dashboard
        dp = dash.get("data_perspective") or {}
        dash["data_perspective"] = dp
        pp = dp.get("price_position") or {}

        computed: Dict[str, Any] = {}
        if trend_result:
            tr = trend_result if isinstance(trend_result, dict) else (
                trend_result.__dict__ if hasattr(trend_result, "__dict__") else {}
            )
            computed["ma5"] = tr.get("ma5")
            computed["ma10"] = tr.get("ma10")
            computed["ma20"] = tr.get("ma20")
            computed["bias_ma5"] = tr.get("bias_ma5")
            computed["current_price"] = tr.get("current_price")
            support_levels = tr.get("support_levels") or []
            resistance_levels = tr.get("resistance_levels") or []
            if support_levels:
                computed["support_level"] = support_levels[0]
            if resistance_levels:
                computed["resistance_level"] = resistance_levels[0]
        if realtime_quote:
            rq = realtime_quote if isinstance(realtime_quote, dict) else (
                realtime_quote.to_dict() if hasattr(realtime_quote, "to_dict") else {}
            )
            if _is_value_placeholder(computed.get("current_price")):
                computed["current_price"] = rq.get("price")

        filled = False
        for k in _PRICE_POS_KEYS:
            if _is_value_placeholder(pp.get(k)) and not _is_value_placeholder(computed.get(k)):
                pp[k] = computed[k]
                filled = True
        if filled:
            dp["price_position"] = pp
            logger.info("[price_position] Filled placeholder fields from computed data")
    except Exception as e:
        logger.warning("[price_position] Fill failed, skipping: %s", e)


def stabilize_decision_with_structure(
    result: "AnalysisResult",
    trend_result: Any = None,
    fundamental_context: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Calibrate aggressive buy/sell advice with price levels and capital flow.

    The LLM can overreact to one-day price movement.  This guard keeps the
    public `decision_type` enum stable while allowing richer neutral wording
    such as 震荡/洗盘观察 when support, resistance, and fund flow do not confirm
    an immediate buy/sell action.
    """
    if not result:
        return

    try:
        language = normalize_report_language(getattr(result, "report_language", "zh"))
        dashboard = result.dashboard if isinstance(result.dashboard, dict) else {}
        data_perspective = dashboard.get("data_perspective") if isinstance(dashboard, dict) else {}
        if not isinstance(data_perspective, dict):
            data_perspective = {}
        price_position = data_perspective.get("price_position")
        if not isinstance(price_position, dict):
            price_position = {}

        trend_dict = _as_dict_for_decision_guard(trend_result)
        current_price = _first_numeric_value(
            getattr(result, "current_price", None),
            price_position.get("current_price"),
            trend_dict.get("current_price"),
        )
        support = _first_numeric_value(
            price_position.get("support_level"),
            _first_list_value(trend_dict.get("support_levels")),
        )
        resistance = _first_numeric_value(
            price_position.get("resistance_level"),
            _first_list_value(trend_dict.get("resistance_levels")),
        )
        decision_type = infer_decision_type_from_advice(
            getattr(result, "decision_type", ""),
            default=getattr(result, "decision_type", "hold") or "hold",
        )
        decision_type = decision_type if decision_type in {"buy", "hold", "sell"} else "hold"
        advice_decision_type = infer_decision_type_from_advice(
            getattr(result, "operation_advice", ""),
            default="",
        )

        flow_bias, flow_reason = _capital_flow_bias_with_status(fundamental_context)
        if flow_bias == "unavailable":
            if isinstance(fundamental_context, dict) and "capital_flow" in fundamental_context:
                if decision_type == "buy" or advice_decision_type == "buy":
                    _downgrade_buy_without_capital_flow(
                        result,
                        language,
                        current_price=current_price,
                        support=support,
                        resistance=resistance,
                        flow_status=flow_reason,
                    )
                else:
                    _set_decision_stability_unavailable(
                        result,
                        language,
                        current_price=current_price,
                        support=support,
                        resistance=resistance,
                        flow_status=flow_reason,
                    )
            return

        if current_price is None:
            return

        broke_support = support is not None and current_price < support * 0.985
        near_support = support is not None and not broke_support and current_price <= support * 1.03
        breakout = resistance is not None and current_price > resistance * 1.01
        near_resistance = (
            resistance is not None
            and not breakout
            and current_price >= resistance * 0.97
        )
        mid_range = (
            support is not None
            and resistance is not None
            and support * 1.03 < current_price < resistance * 0.97
        )

        has_significant_risk = _has_structural_risk_alert(result)

        if decision_type == "buy":
            if near_resistance and flow_bias != "inflow":
                _downgrade_to_structural_hold(
                    result,
                    language,
                    advice_key="range",
                    reason_key="buy_near_resistance",
                    current_price=current_price,
                    support=support,
                    resistance=resistance,
                    flow_bias=flow_bias,
                )
            elif flow_bias == "outflow" and not breakout:
                _downgrade_to_structural_hold(
                    result,
                    language,
                    advice_key="range",
                    reason_key="buy_with_outflow",
                    current_price=current_price,
                    support=support,
                    resistance=resistance,
                    flow_bias=flow_bias,
                )
            elif mid_range and flow_bias == "neutral":
                _downgrade_to_structural_hold(
                    result,
                    language,
                    advice_key="range",
                    reason_key="hold_mid_range",
                    current_price=current_price,
                    support=support,
                    resistance=resistance,
                    flow_bias=flow_bias,
                )
        elif decision_type == "sell":
            if near_support and (flow_bias != "outflow") and not has_significant_risk:
                _downgrade_to_structural_hold(
                    result,
                    language,
                    advice_key="shakeout",
                    reason_key="sell_near_support",
                    current_price=current_price,
                    support=support,
                    resistance=resistance,
                    flow_bias=flow_bias,
                )
            elif flow_bias == "inflow" and not broke_support and not has_significant_risk:
                _downgrade_to_structural_hold(
                    result,
                    language,
                    advice_key="hold",
                    reason_key="sell_with_inflow",
                    current_price=current_price,
                    support=support,
                    resistance=resistance,
                    flow_bias=flow_bias,
                )
        elif decision_type == "hold":
            change_pct = _first_numeric_value(getattr(result, "change_pct", None))
            if change_pct is not None and change_pct < 0 and near_support and flow_bias != "outflow":
                _set_structural_hold_wording(
                    result,
                    language,
                    advice_key="shakeout",
                    reason_key="hold_shakeout",
                    current_price=current_price,
                    support=support,
                    resistance=resistance,
                    flow_bias=flow_bias,
                )
            elif mid_range and flow_bias == "neutral":
                _set_structural_hold_wording(
                    result,
                    language,
                    advice_key="range",
                    reason_key="hold_mid_range",
                    current_price=current_price,
                    support=support,
                    resistance=resistance,
                    flow_bias=flow_bias,
                )
        _sync_stability_dashboard_fields(result)
    except Exception as exc:
        logger.warning("[decision_stability] skipped: %s", exc)


def _has_structural_risk_alert(result: "AnalysisResult") -> bool:
    dashboard = result.dashboard if isinstance(result.dashboard, dict) else {}

    risk_text = getattr(result, "risk_warning", "")
    if _is_significant_structural_risk(risk_text):
        return True

    intelligence = dashboard.get("intelligence") if isinstance(dashboard, dict) else None
    if isinstance(intelligence, dict):
        risk_alerts = intelligence.get("risk_alerts")
        if isinstance(risk_alerts, str):
            if _is_significant_structural_risk(risk_alerts):
                return True
        elif isinstance(risk_alerts, (list, tuple, set)):
            if any(_is_significant_structural_risk(item) for item in risk_alerts):
                return True

    core_conclusion = dashboard.get("core_conclusion") if isinstance(dashboard, dict) else None
    if isinstance(core_conclusion, dict):
        signal_type = str(core_conclusion.get("signal_type", "")).strip()
        if _is_significant_structural_risk(signal_type):
            return True
    return False


def _is_significant_structural_risk(value: Any) -> bool:
    text = str(value or "").strip()
    if not _is_meaningful_text(text):
        return False

    normalized = text.lower()
    if any(keyword in normalized for keyword in _STRUCTURAL_RISK_PHRASE_HINTS):
        return True

    return "重大" in text and "风险" in normalized


def _sync_stability_dashboard_fields(result: "AnalysisResult") -> None:
    dashboard = result.dashboard if isinstance(result.dashboard, dict) else {}
    result.dashboard = dashboard
    dashboard["sentiment_score"] = getattr(result, "sentiment_score", None)
    dashboard["operation_advice"] = getattr(result, "operation_advice", None)
    dashboard["decision_type"] = getattr(result, "decision_type", None)


def _as_dict_for_decision_guard(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        try:
            converted = value.to_dict()
            return converted if isinstance(converted, dict) else {}
        except Exception:
            return {}
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {}


def _first_list_value(value: Any) -> Any:
    if isinstance(value, (list, tuple)) and value:
        return value[0]
    return value


def _coerce_numeric_value(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        if math.isfinite(float(value)):
            return float(value)
        return None
    text = str(value).replace(",", "").replace("，", "").strip()
    if not text or text.upper() in {"N/A", "NA", "NONE", "NULL"}:
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _first_numeric_value(*values: Any) -> Optional[float]:
    for value in values:
        if isinstance(value, (list, tuple)):
            nested = _first_numeric_value(*value)
            if nested is not None:
                return nested
            continue
        numeric = _coerce_numeric_value(value)
        if numeric is not None:
            return numeric
    return None


def _capital_flow_bias(fundamental_context: Optional[Dict[str, Any]]) -> str:
    return _capital_flow_bias_with_status(fundamental_context)[0]


def _capital_flow_bias_with_status(
    fundamental_context: Optional[Dict[str, Any]],
) -> tuple[str, str]:
    if not isinstance(fundamental_context, dict):
        return "unavailable", "invalid_context"
    block = fundamental_context.get("capital_flow")
    if not isinstance(block, dict):
        return "unavailable", "capital_flow_block_missing"
    status = str(block.get("status") or "").strip().lower()
    normalized_status = status.replace("-", " ").replace("_", " ").strip()
    if normalized_status in _CAPITAL_FLOW_UNAVAILABLE_STATUS or "not supported" in normalized_status:
        return "unavailable", status or "not_supported"
    data = block.get("data") if isinstance(block.get("data"), dict) else block
    stock_flow = data.get("stock_flow") if isinstance(data, dict) else None
    if not isinstance(stock_flow, dict) or not stock_flow:
        return "unavailable", "empty_stock_flow"

    def _flow_direction(value: Optional[float]) -> Optional[str]:
        if value is None or value == 0:
            return None
        return "inflow" if value > 0 else "outflow"

    numeric_values = [
        _coerce_numeric_value(stock_flow.get("main_net_inflow")),
        _coerce_numeric_value(stock_flow.get("inflow_5d")),
        _coerce_numeric_value(stock_flow.get("inflow_10d")),
    ]
    if all(value is None for value in numeric_values):
        return "unavailable", "missing_or_na_flow_fields"

    ordered_signals = [
        _flow_direction(value) for value in numeric_values
    ]
    directions = {signal for signal in ordered_signals if signal is not None}
    if not directions or len(directions) > 1:
        return "neutral", "conflict_or_missing"
    for signal in ordered_signals:
        if signal is not None:
            return signal, "ok"
    return "neutral", "neutral"


def _capital_flow_status_for_stability(reason: str, language: str) -> str:
    normalized = str(reason or "").strip().lower()
    if "not_supported" in normalized or "unsupported" in normalized or "not available" in normalized:
        return "市场资金流服务暂不支持" if language == "zh" else "Capital flow source unsupported"
    if "empty_stock_flow" in normalized or "missing" in normalized:
        return "资金流数据缺失" if language == "zh" else "capital flow data unavailable"
    return "资金流数据不可用" if language == "zh" else "capital flow unavailable"


def _set_decision_stability_unavailable(
    result: "AnalysisResult",
    language: str,
    *,
    current_price: Optional[float],
    support: Optional[float],
    resistance: Optional[float],
    flow_status: str,
) -> None:
    dashboard = result.dashboard if isinstance(result.dashboard, dict) else {}
    result.dashboard = dashboard
    dashboard["decision_stability"] = {
        "applied": False,
        "reason": "资金流不可用，未使用资金流校准" if language == "zh" else "Capital flow unavailable; stability calibration not applied",
        "capital_flow_status": _capital_flow_status_for_stability(flow_status, language),
        "current_price": current_price,
        "support": support,
        "resistance": resistance,
        "capital_flow_bias": "unavailable",
    }
    _sync_stability_dashboard_fields(result)


def _bound_hold_watch_sentiment_score(result: "AnalysisResult") -> None:
    try:
        score = int(getattr(result, "sentiment_score", 50))
    except (TypeError, ValueError):
        score = 50
    result.sentiment_score = min(59, max(45, score))


def _apply_hold_watch_dashboard(
    result: "AnalysisResult",
    language: str,
    *,
    advice: str,
    reason: str,
    current_price: Optional[float],
    support: Optional[float],
    resistance: Optional[float],
    flow_bias: str,
    no_position: str,
    has_position: str,
    capital_flow_status: Optional[str] = None,
) -> None:
    result.operation_advice = advice

    dashboard = result.dashboard if isinstance(result.dashboard, dict) else {}
    result.dashboard = dashboard
    core = dashboard.get("core_conclusion")
    if not isinstance(core, dict):
        core = {}
        dashboard["core_conclusion"] = core
    core["signal_type"] = "🟡持有观望" if language == "zh" else "🟡 Hold / Watch"
    core["one_sentence"] = f"{advice}：{reason}" if language == "zh" else f"{advice}: {reason}"

    position_advice = core.get("position_advice")
    if not isinstance(position_advice, dict):
        position_advice = {}
        core["position_advice"] = position_advice
    position_advice["no_position"] = no_position
    position_advice["has_position"] = has_position

    stability = {
        "applied": True,
        "reason": reason,
        "current_price": current_price,
        "support": support,
        "resistance": resistance,
        "capital_flow_bias": flow_bias,
    }
    if capital_flow_status is not None:
        stability["capital_flow_status"] = capital_flow_status
    dashboard["decision_stability"] = stability

    if reason and reason not in str(result.risk_warning or ""):
        sep = "；" if language == "zh" else "; "
        result.risk_warning = f"{result.risk_warning}{sep}{reason}" if result.risk_warning else reason
    result.buy_reason = reason or result.buy_reason


def _downgrade_buy_without_capital_flow(
    result: "AnalysisResult",
    language: str,
    *,
    current_price: Optional[float],
    support: Optional[float],
    resistance: Optional[float],
    flow_status: str,
) -> None:
    status_text = _capital_flow_status_for_stability(flow_status, language)
    if language == "zh":
        advice = "持有观察"
        reason = f"{status_text}，买入结论缺少资金面确认，先按观察处理。"
        no_position = "空仓先不追买，等待资金流恢复、支撑确认或有效突破后再行动。"
        has_position = "持仓以关键支撑为风控线，资金流恢复前控制仓位。"
        confidence = "低"
    else:
        advice = "Hold and watch"
        reason = f"{status_text}; the buy call lacks capital-flow confirmation, so treat it as watch-only."
        no_position = "Do not chase; wait for capital-flow recovery, support confirmation, or a valid breakout."
        has_position = "Use key support as the risk line and keep position size controlled until capital flow recovers."
        confidence = "Low"

    result.decision_type = "hold"
    result.confidence_level = confidence
    _bound_hold_watch_sentiment_score(result)
    _apply_hold_watch_dashboard(
        result,
        language,
        advice=advice,
        reason=reason,
        current_price=current_price,
        support=support,
        resistance=resistance,
        flow_bias="unavailable",
        no_position=no_position,
        has_position=has_position,
        capital_flow_status=status_text,
    )
    _sync_stability_dashboard_fields(result)
    logger.info("[decision_stability] Downgraded buy because capital flow is unavailable: %s", flow_status)


def _downgrade_to_structural_hold(
    result: "AnalysisResult",
    language: str,
    *,
    advice_key: str,
    reason_key: str,
    current_price: float,
    support: Optional[float],
    resistance: Optional[float],
    flow_bias: str,
) -> None:
    result.decision_type = "hold"
    _bound_hold_watch_sentiment_score(result)
    _set_structural_hold_wording(
        result,
        language,
        advice_key=advice_key,
        reason_key=reason_key,
        current_price=current_price,
        support=support,
        resistance=resistance,
        flow_bias=flow_bias,
    )


def _set_structural_hold_wording(
    result: "AnalysisResult",
    language: str,
    *,
    advice_key: str,
    reason_key: str,
    current_price: float,
    support: Optional[float],
    resistance: Optional[float],
    flow_bias: str,
) -> None:
    advice = {
        "zh": {
            "range": "震荡观望",
            "shakeout": "洗盘观察",
            "hold": "持有观察",
        },
        "en": {
            "range": "Range-bound watch",
            "shakeout": "Shakeout watch",
            "hold": "Hold and watch",
        },
    }[language].get(advice_key, "持有观察" if language == "zh" else "Hold and watch")
    reason_templates = {
        "zh": {
            "buy_near_resistance": "价格接近压力位且主力资金未确认流入，不宜仅因短线反弹追买。",
            "buy_with_outflow": "主力资金流出与买入结论冲突，买点需等待支撑确认或资金回流。",
            "sell_near_support": "价格贴近支撑且未见资金持续流出，不宜仅因单日下跌直接卖出。",
            "sell_with_inflow": "主力资金流入与卖出结论冲突，先按持有观察处理并跟踪支撑失效。",
            "hold_shakeout": "价格回落至支撑附近但资金未确认流出，更适合按洗盘观察处理。",
            "hold_mid_range": "价格处于支撑与压力之间且资金流不明确，维持震荡观望更可操作。",
        },
        "en": {
            "buy_near_resistance": "Price is near resistance without confirmed main-force inflow, so chasing the rebound is not actionable.",
            "buy_with_outflow": "Main-force outflow conflicts with a buy call; wait for support confirmation or capital inflow.",
            "sell_near_support": "Price is near support without sustained outflow, so a one-day drop is not enough to sell.",
            "sell_with_inflow": "Main-force inflow conflicts with a sell call; hold and watch for support failure.",
            "hold_shakeout": "Price pulled back near support without confirmed outflow, which is better treated as a shakeout watch.",
            "hold_mid_range": "Price is between support and resistance with neutral fund flow, so range-bound watch is more actionable.",
        },
    }
    reason = reason_templates[language].get(reason_key, "")
    result.operation_advice = advice
    if language == "zh" and "震荡" not in str(result.trend_prediction) and advice_key == "range":
        result.trend_prediction = "震荡"
    elif language == "en" and advice_key == "range":
        result.trend_prediction = "Sideways"

    if language == "zh":
        no_position = "空仓先不追涨杀跌，等待支撑确认、放量突破或资金回流后再行动。"
        has_position = "持仓以关键支撑为风控线，未跌破前以观察和分批控仓为主。"
    else:
        no_position = "Do not chase or panic; wait for support confirmation, breakout, or renewed inflow."
        has_position = "Use key support as the risk line and manage position size unless support fails."
    _apply_hold_watch_dashboard(
        result,
        language,
        advice=advice,
        reason=reason,
        current_price=current_price,
        support=support,
        resistance=resistance,
        flow_bias=flow_bias,
        no_position=no_position,
        has_position=has_position,
    )
    logger.info("[decision_stability] Applied structural hold calibration: %s", reason_key)


def get_stock_name_multi_source(
    stock_code: str,
    context: Optional[Dict] = None,
    data_manager = None
) -> str:
    """
    多来源获取股票中文名称

    获取策略（按优先级）：
    1. 从传入的 context 中获取（realtime 数据）
    2. 从静态映射表 STOCK_NAME_MAP 获取
    3. 从 DataFetcherManager 获取（各数据源）
    4. 返回默认名称（股票+代码）

    Args:
        stock_code: 股票代码
        context: 分析上下文（可选）
        data_manager: DataFetcherManager 实例（可选）

    Returns:
        股票中文名称
    """
    # 1. 从上下文获取（实时行情数据）
    if context:
        # 优先从 stock_name 字段获取
        if context.get('stock_name'):
            name = context['stock_name']
            if name and not name.startswith('股票'):
                return name

        # 其次从 realtime 数据获取
        if 'realtime' in context and context['realtime'].get('name'):
            return context['realtime']['name']

    # 2. 从静态映射表获取
    if stock_code in STOCK_NAME_MAP:
        return STOCK_NAME_MAP[stock_code]

    # 3. 从数据源获取
    if data_manager is None:
        try:
            from data_provider.base import DataFetcherManager
            data_manager = DataFetcherManager()
        except Exception as e:
            logger.debug(f"无法初始化 DataFetcherManager: {e}")

    if data_manager:
        try:
            name = data_manager.get_stock_name(stock_code)
            if name:
                # 更新缓存
                STOCK_NAME_MAP[stock_code] = name
                return name
        except Exception as e:
            logger.debug(f"从数据源获取股票名称失败: {e}")

    # 4. 返回默认名称
    return f'股票{stock_code}'


@dataclass
class AnalysisResult:
    """
    AI 分析结果数据类 - 决策仪表盘版

    封装 Gemini 返回的分析结果，包含决策仪表盘和详细分析
    """
    code: str
    name: str

    # ========== 核心指标 ==========
    sentiment_score: int  # 综合评分 0-100 (>70强烈看多, >60看多, 40-60震荡, <40看空)
    trend_prediction: str  # 趋势预测：强烈看多/看多/震荡/看空/强烈看空
    operation_advice: str  # 操作建议：买入/加仓/持有/减仓/卖出/观望
    decision_type: str = "hold"  # 决策类型：buy/hold/sell（用于统计）
    confidence_level: str = "中"  # 置信度：高/中/低
    report_language: str = "zh"  # 报告输出语言：zh/en
    analysis_mode: str = "daily"  # 分析模式：daily/hourly
    analysis_timeframe: str = "日线"  # 分析周期展示标签：日线/小时线
    action: Optional[str] = None  # 建议动作 taxonomy：buy/add/hold/reduce/sell/watch/avoid/alert
    action_label: Optional[str] = None  # 本地化建议动作标签

    # ========== 决策仪表盘 (新增) ==========
    dashboard: Optional[Dict[str, Any]] = None  # 完整的决策仪表盘数据

    # ========== 走势分析 ==========
    trend_analysis: str = ""  # 走势形态分析（支撑位、压力位、趋势线等）
    short_term_outlook: str = ""  # 短期展望（1-3日）
    medium_term_outlook: str = ""  # 中期展望（1-2周）

    # ========== 技术面分析 ==========
    technical_analysis: str = ""  # 技术指标综合分析
    ma_analysis: str = ""  # 均线分析（多头/空头排列，金叉/死叉等）
    volume_analysis: str = ""  # 量能分析（放量/缩量，主力动向等）
    pattern_analysis: str = ""  # K线形态分析

    # ========== 基本面分析 ==========
    fundamental_analysis: str = ""  # 基本面综合分析
    sector_position: str = ""  # 板块地位和行业趋势
    company_highlights: str = ""  # 公司亮点/风险点

    # ========== 情绪面/消息面分析 ==========
    news_summary: str = ""  # 近期重要新闻/公告摘要
    market_sentiment: str = ""  # 市场情绪分析
    hot_topics: str = ""  # 相关热点话题

    # ========== 综合分析 ==========
    analysis_summary: str = ""  # 综合分析摘要
    key_points: str = ""  # 核心看点（3-5个要点）
    risk_warning: str = ""  # 风险提示
    buy_reason: str = ""  # 买入/卖出理由

    # ========== 元数据 ==========
    market_snapshot: Optional[Dict[str, Any]] = None  # 当日行情快照（展示用）
    raw_response: Optional[str] = None  # 原始响应（调试用）
    search_performed: bool = False  # 是否执行了联网搜索
    data_sources: str = ""  # 数据来源说明
    success: bool = True
    error_message: Optional[str] = None

    # ========== 价格数据（分析时快照）==========
    current_price: Optional[float] = None  # 分析时的股价
    change_pct: Optional[float] = None     # 分析时的涨跌幅(%)

    # ========== 模型标记（Issue #528）==========
    model_used: Optional[str] = None  # 分析使用的 LLM 模型（完整名，如 gemini/gemini-2.0-flash）

    # ========== 历史对比（Report Engine P0）==========
    query_id: Optional[str] = None  # 本次分析 query_id，用于历史对比时排除本次记录

    # ========== 基本面上下文（仅运行时，用于通知拼装；不持久化到 to_dict）==========
    fundamental_context: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'code': self.code,
            'name': self.name,
            'sentiment_score': self.sentiment_score,
            'trend_prediction': self.trend_prediction,
            'operation_advice': self.operation_advice,
            'decision_type': self.decision_type,
            'confidence_level': self.confidence_level,
            'report_language': self.report_language,
            'analysis_mode': self.analysis_mode,
            'analysis_timeframe': self.analysis_timeframe,
            'action': self.action,
            'action_label': self.action_label,
            'dashboard': self.dashboard,  # 决策仪表盘数据
            'trend_analysis': self.trend_analysis,
            'short_term_outlook': self.short_term_outlook,
            'medium_term_outlook': self.medium_term_outlook,
            'technical_analysis': self.technical_analysis,
            'ma_analysis': self.ma_analysis,
            'volume_analysis': self.volume_analysis,
            'pattern_analysis': self.pattern_analysis,
            'fundamental_analysis': self.fundamental_analysis,
            'sector_position': self.sector_position,
            'company_highlights': self.company_highlights,
            'news_summary': self.news_summary,
            'market_sentiment': self.market_sentiment,
            'hot_topics': self.hot_topics,
            'analysis_summary': self.analysis_summary,
            'key_points': self.key_points,
            'risk_warning': self.risk_warning,
            'buy_reason': self.buy_reason,
            'market_snapshot': self.market_snapshot,
            'search_performed': self.search_performed,
            'success': self.success,
            'error_message': self.error_message,
            'current_price': self.current_price,
            'change_pct': self.change_pct,
            'model_used': self.model_used,
        }

    def get_core_conclusion(self) -> str:
        """获取核心结论（一句话）"""
        if self.dashboard and 'core_conclusion' in self.dashboard:
            return self.dashboard['core_conclusion'].get('one_sentence', self.analysis_summary)
        return self.analysis_summary

    def get_position_advice(self, has_position: bool = False) -> str:
        """获取持仓建议"""
        if self.dashboard and 'core_conclusion' in self.dashboard:
            pos_advice = self.dashboard['core_conclusion'].get('position_advice', {})
            if has_position:
                return pos_advice.get('has_position', self.operation_advice)
            return pos_advice.get('no_position', self.operation_advice)
        return self.operation_advice

    def get_sniper_points(self) -> Dict[str, str]:
        """获取狙击点位"""
        if self.dashboard and 'battle_plan' in self.dashboard:
            return self.dashboard['battle_plan'].get('sniper_points', {})
        return {}

    def get_checklist(self) -> List[str]:
        """获取检查清单"""
        if self.dashboard and 'battle_plan' in self.dashboard:
            return self.dashboard['battle_plan'].get('action_checklist', [])
        return []

    def get_risk_alerts(self) -> List[str]:
        """获取风险警报"""
        if self.dashboard and 'intelligence' in self.dashboard:
            return self.dashboard['intelligence'].get('risk_alerts', [])
        return []

    def get_emoji(self) -> str:
        """根据操作建议返回对应 emoji"""
        _, emoji, _ = get_signal_level(
            self.operation_advice,
            self.sentiment_score,
            self.report_language,
        )
        return emoji

    def get_confidence_stars(self) -> str:
        """返回置信度星级"""
        star_map = {
            "高": "⭐⭐⭐",
            "high": "⭐⭐⭐",
            "中": "⭐⭐",
            "medium": "⭐⭐",
            "低": "⭐",
            "low": "⭐",
        }
        return star_map.get(str(self.confidence_level or "").strip().lower(), "⭐⭐")


def populate_decision_action_fields(
    result: AnalysisResult,
    *,
    explicit_action: Any = None,
    report_type: Any = None,
    use_existing_action: bool = True,
) -> AnalysisResult:
    """Populate optional decision action fields without changing legacy advice."""

    action_source = explicit_action
    if action_source is None and use_existing_action:
        action_source = getattr(result, "action", None)

    fields = build_action_fields(
        operation_advice=getattr(result, "operation_advice", None),
        explicit_action=action_source,
        report_type=report_type,
        report_language=getattr(result, "report_language", "zh"),
    )
    result.action = fields["action"]
    result.action_label = fields["action_label"]
    return result


def _validate_btc_execution_ladder(payload: dict[str, Any]) -> list[str]:
    """Validate numeric invariants between an optional staged plan and its trade plan."""
    if "execution_ladder" not in payload:
        return []

    ladder = payload.get("execution_ladder")
    if not isinstance(ladder, dict):
        return ["execution_ladder_invalid"]

    errors: list[str] = []
    current_action = str(ladder.get("current_action") or "").strip().lower()
    if current_action not in {"trial", "wait"}:
        errors.append("execution_ladder_current_action_invalid")

    trial = ladder.get("trial_entry")
    if not isinstance(trial, dict):
        errors.append("execution_ladder_trial_entry_missing")
    else:
        plan_entry = parse_sniper_value(payload.get("entry_price"))
        trial_entry = parse_sniper_value(trial.get("entry_price"))
        if trial_entry is None:
            errors.append("execution_ladder_trial_entry_price_missing")
        elif plan_entry is not None and abs(plan_entry - trial_entry) > max(1e-6, abs(plan_entry) * 1e-8):
            errors.append("execution_ladder_trial_entry_price_mismatch")

    invalidation = ladder.get("invalidation")
    if not isinstance(invalidation, dict):
        errors.append("execution_ladder_invalidation_missing")
    else:
        stop_loss = parse_sniper_value(payload.get("stop_loss"))
        invalidation_price = parse_sniper_value(invalidation.get("price"))
        if invalidation_price is None:
            errors.append("execution_ladder_invalidation_price_missing")
        elif stop_loss is not None and abs(stop_loss - invalidation_price) > max(1e-6, abs(stop_loss) * 1e-8):
            errors.append("execution_ladder_invalidation_price_mismatch")

    confirmation_add = ladder.get("confirmation_add")
    if not isinstance(confirmation_add, dict):
        errors.append("execution_ladder_confirmation_add_missing")
    return errors


def _normalize_btc_risk_reward(
    payload: dict[str, Any],
    *,
    direction: str,
    language: str,
) -> None:
    """Replace model-authored risk/reward prose with deterministic arithmetic."""
    entry = parse_sniper_value(payload.get("entry_price"))
    stop = parse_sniper_value(payload.get("stop_loss"))
    target = parse_sniper_value(payload.get("take_profit"))
    if entry is None or stop is None or target is None:
        return
    risk = entry - stop if direction == "long" else stop - entry
    reward = target - entry if direction == "long" else entry - target
    if risk <= 0 or reward <= 0:
        return
    ratio = reward / risk
    payload["calculated_risk_reward"] = round(ratio, 4)
    if language == "zh":
        payload["risk_reward"] = (
            f"1:{ratio:.2f}（按入场 {entry:g}、止损 {stop:g}、止盈 {target:g} 重新计算）"
        )
    else:
        payload["risk_reward"] = (
            f"1:{ratio:.2f} (recalculated from entry {entry:g}, stop {stop:g}, target {target:g})"
        )


def _annotate_btc_target_mfe_calibration(
    payload: dict[str, Any],
    *,
    direction: str,
    horizon: str,
    calibration: Optional[Dict[str, Any]],
    language: str,
) -> None:
    """Annotate take-profit distance against historical signal MFE percentiles.

    Diagnostic only: the annotation never changes direction, enabled state or
    validation outcome (same annotate-not-downgrade contract as validation).
    """
    if not isinstance(calibration, dict):
        return
    stats = calibration.get(horizon)
    if not isinstance(stats, dict):
        return
    try:
        sample_count = int(stats.get("sample_count") or 0)
    except (TypeError, ValueError):
        return
    if sample_count < _BTC_MFE_MIN_SAMPLE_COUNT:
        return
    mfe_p70 = stats.get("mfe_p70_pct")
    if not isinstance(mfe_p70, (int, float)) or mfe_p70 <= 0:
        return
    entry = parse_sniper_value(payload.get("entry_price"))
    target = parse_sniper_value(payload.get("take_profit"))
    if entry is None or entry <= 0 or target is None:
        return
    distance = target - entry if direction == "long" else entry - target
    if distance <= 0:
        return
    target_distance_pct = distance / entry * 100.0
    beyond = target_distance_pct > float(mfe_p70)
    annotation: dict[str, Any] = {
        "status": "beyond_typical_mfe" if beyond else "within_typical_mfe",
        "target_distance_pct": round(target_distance_pct, 4),
        "mfe_p50_pct": stats.get("mfe_p50_pct"),
        "mfe_p70_pct": stats.get("mfe_p70_pct"),
        "sample_count": stats.get("sample_count"),
    }
    if beyond:
        annotation["note"] = (
            f"止盈距离 {target_distance_pct:.2f}% 超过同周期历史信号 MFE P70（{float(mfe_p70):.2f}%），"
            "按历史波动该目标较难在持有窗口内达成，建议核对目标价或持有窗口。"
            if language == "zh"
            else (
                f"Target distance {target_distance_pct:.2f}% exceeds the historical signal MFE P70 "
                f"({float(mfe_p70):.2f}%) for this horizon; the target is statistically hard to reach "
                "within the holding window — review the target or holding window."
            )
        )
    else:
        annotation["note"] = (
            f"止盈距离 {target_distance_pct:.2f}% 在同周期历史信号 MFE P70（{float(mfe_p70):.2f}%）以内。"
            if language == "zh"
            else (
                f"Target distance {target_distance_pct:.2f}% sits within the historical signal MFE P70 "
                f"({float(mfe_p70):.2f}%) for this horizon."
            )
        )
    payload["target_calibration"] = annotation


def align_btc_execution_plans(
    result: AnalysisResult,
    *,
    runtime_config: Any,
    trigger_context: Optional[Dict[str, Any]] = None,
    technical_context: Optional[Dict[str, Any]] = None,
    mfe_calibration: Optional[Dict[str, Any]] = None,
) -> AnalysisResult:
    """Annotate BTC plans with execution-validation results.

    Plans that fail the active execution contract keep their original
    direction and advice; validation outcome is surfaced via
    ``validation_status`` / ``validation_errors`` / ``validation_note``
    so users can judge for themselves instead of the analyzer silently
    downgrading everything to "watch".
    """

    dashboard = result.dashboard if isinstance(result.dashboard, dict) else {}
    battle_plan = dashboard.get("battle_plan") if isinstance(dashboard, dict) else None
    if not isinstance(battle_plan, dict):
        return result

    _apply_btc_trigger_execution_guard(
        battle_plan,
        trigger_context=trigger_context,
        language=normalize_report_language(result.report_language),
    )
    _apply_btc_intraday_alignment_guard(
        battle_plan,
        technical_context=technical_context,
        language=normalize_report_language(result.report_language),
    )
    _apply_btc_volatility_risk_overlay(
        battle_plan,
        technical_context=technical_context,
        language=normalize_report_language(result.report_language),
    )
    _apply_btc_tradeability_guard(
        battle_plan,
        technical_context=technical_context,
        language=normalize_report_language(result.report_language),
    )

    if str(getattr(runtime_config, "crypto_backtest_engine_version", "")).strip().lower() != "btc-plan-v5":
        return result

    config = CryptoPlanBacktestConfig(
        neutral_band_pct=float(getattr(runtime_config, "crypto_backtest_neutral_band_pct", 0.2)),
        engine_version="btc-plan-v5",
        initial_equity=float(getattr(runtime_config, "crypto_backtest_initial_equity", 10000.0)),
        risk_per_trade_pct=float(getattr(runtime_config, "crypto_backtest_risk_per_trade_pct", 1.0)),
        max_notional_pct=float(getattr(runtime_config, "crypto_backtest_max_notional_pct", 100.0)),
        leverage=float(getattr(runtime_config, "crypto_backtest_leverage", 1.0)),
        fee_rate_bps=float(getattr(runtime_config, "crypto_backtest_fee_rate_bps", 5.0)),
        slippage_bps=float(getattr(runtime_config, "crypto_backtest_slippage_bps", 2.0)),
        maker_fee_rate_bps=float(getattr(runtime_config, "crypto_backtest_maker_fee_rate_bps", 2.0)),
        taker_fee_rate_bps=float(getattr(runtime_config, "crypto_backtest_taker_fee_rate_bps", 5.0)),
        maintenance_margin_rate=float(getattr(runtime_config, "crypto_backtest_maintenance_margin_rate", 0.005)),
        minimum_risk_reward=float(getattr(runtime_config, "crypto_backtest_minimum_risk_reward", 1.2)),
        minimum_volume_ratio=float(getattr(runtime_config, "crypto_backtest_minimum_volume_ratio", 1.0)),
    )
    plans = (
        ("long_plan", "daily_long", "daily", "long"),
        ("short_plan", "daily_short", "daily", "short"),
        ("intraday_plan", "intraday", "intraday", "wait"),
    )
    language = normalize_report_language(result.report_language)

    for key, plan_type, horizon, default_direction in plans:
        payload = battle_plan.get(key)
        if not isinstance(payload, dict):
            continue
        direction = str(payload.get("direction") or default_direction).strip().lower()
        if key == "intraday_plan" and payload.get("enabled") is False:
            direction = "wait"
        if direction not in {"long", "short"}:
            if direction in {"wait", "none", ""}:
                payload["validation_status"] = "skipped"
                payload["validation_errors"] = []
                wait_reason = str(payload.get("no_trade_reason") or "").strip()
                if language == "zh":
                    payload["validation_note"] = (
                        f"等待状态，无需执行校验。{wait_reason}"
                        if wait_reason
                        else "等待状态，无需执行校验"
                    )
                else:
                    payload["validation_note"] = (
                        f"wait state, execution validation skipped. {wait_reason}"
                        if wait_reason
                        else "wait state, execution validation skipped"
                    )
            else:
                payload["validation_status"] = "failed"
                payload["validation_errors"] = ["unsupported_direction"]
                payload["validation_note"] = (
                    "未通过执行校验（unsupported_direction）：方向需为 long/short/wait。"
                    if language == "zh"
                    else "Execution validation failed (unsupported_direction): direction must be long/short/wait."
                )
            continue

        plan = CryptoPlan(
            plan_type=plan_type,
            horizon=horizon,
            direction=direction,
            entry_price=parse_sniper_value(payload.get("entry_price")),
            stop_loss=parse_sniper_value(payload.get("stop_loss")),
            take_profit=parse_sniper_value(payload.get("take_profit")),
            raw_plan=dict(payload),
            execution_contract=(
                dict(payload["execution_contract"])
                if isinstance(payload.get("execution_contract"), dict)
                else None
            ),
            position_multiplier_cap=_coerce_position_multiplier_cap(
                payload.get("position_multiplier_cap"),
            ),
        )
        _normalize_btc_risk_reward(payload, direction=direction, language=language)
        _annotate_btc_target_mfe_calibration(
            payload,
            direction=direction,
            horizon=horizon,
            calibration=mfe_calibration,
            language=language,
        )
        if "position_multiplier_cap" in payload:
            payload["position_multiplier_cap"] = plan.position_multiplier_cap
        errors = CryptoBacktestEngine.validate_execution_plan(plan=plan, config=config)
        errors.extend(_validate_btc_execution_ladder(payload))
        if not errors:
            payload["validation_status"] = "passed"
            payload["validation_errors"] = []
            payload["validation_note"] = (
                "已通过执行校验" if language == "zh" else "passed execution validation"
            )
            continue

        error_text = ", ".join(errors)
        payload["validation_status"] = "failed"
        payload["validation_errors"] = errors
        payload["validation_note"] = (
            f"未通过执行校验（{error_text}），计划仅供参考，请核对点位与风控参数后再执行。"
            if language == "zh"
            else (
                f"Execution validation failed ({error_text}); plan is informational only — "
                "verify levels and risk parameters before executing."
            )
        )

    return result


def _coerce_position_multiplier_cap(value: Any, *, default: float = 1.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    if not math.isfinite(parsed):
        parsed = default
    return max(0.0, min(parsed, 1.0))


def _apply_btc_tradeability_guard(
    battle_plan: Dict[str, Any],
    *,
    technical_context: Optional[Dict[str, Any]],
    language: str,
) -> None:
    """Translate high-risk indicator conflicts and missing context into executable state."""
    plan_directions = {
        "long_plan": "long",
        "short_plan": "short",
        "intraday_plan": "wait",
    }
    plan_keys = tuple(plan_directions)
    for plan_key, default_direction in plan_directions.items():
        payload = battle_plan.get(plan_key)
        if not isinstance(payload, dict):
            continue
        existing_audit = payload.get("tradeability_audit")
        if (
            isinstance(existing_audit, dict)
            and existing_audit.get("version") == _BTC_TRADEABILITY_GUARD_VERSION
            and isinstance(existing_audit.get("original_plan"), dict)
        ):
            continue
        payload["tradeability_audit"] = {
            "version": _BTC_TRADEABILITY_GUARD_VERSION,
            "applied": False,
            "decision": "not_evaluated",
            "reasons": [],
            "original_plan": {
                "enabled": payload.get("enabled"),
                "direction": payload.get("direction") or default_direction,
                "entry_price": payload.get("entry_price"),
                "stop_loss": payload.get("stop_loss"),
                "take_profit": payload.get("take_profit"),
                "execution_contract": copy.deepcopy(payload.get("execution_contract")),
                "position_multiplier_cap": payload.get("position_multiplier_cap"),
            },
        }

    if not isinstance(technical_context, dict):
        for plan_key in plan_keys:
            payload = battle_plan.get(plan_key)
            audit = payload.get("tradeability_audit") if isinstance(payload, dict) else None
            if isinstance(audit, dict):
                audit["reasons"] = ["technical_context_missing"]
        return
    timeframes = technical_context.get("timeframes")
    timeframes = timeframes if isinstance(timeframes, dict) else {}
    daily = timeframes.get("daily") if isinstance(timeframes.get("daily"), dict) else technical_context
    hourly = timeframes.get("hourly") if isinstance(timeframes.get("hourly"), dict) else {}
    intraday = technical_context.get("intraday")
    intraday = intraday if isinstance(intraday, dict) else {}

    def text(payload: Dict[str, Any], *path: str) -> str:
        current: Any = payload
        for key in path:
            if not isinstance(current, dict):
                return ""
            current = current.get(key)
        return str(current or "").strip().lower()

    def disable(payload: Dict[str, Any], reason_key: str, reason_zh: str, reason_en: str) -> None:
        reason = reason_zh if language == "zh" else reason_en
        payload["enabled"] = False
        payload["direction"] = "wait"
        payload["tradeability_status"] = "blocked"
        payload["tradeability_reasons"] = [reason_key]
        payload["no_trade_reason"] = reason
        payload["reason"] = reason
        ladder = payload.get("execution_ladder")
        if isinstance(ladder, dict):
            ladder["current_action"] = "wait"
            trial_entry = ladder.get("trial_entry")
            if isinstance(trial_entry, dict):
                trial_entry["enabled"] = False

    for plan_key, source in (("long_plan", daily), ("intraday_plan", hourly or daily)):
        payload = battle_plan.get(plan_key)
        if not isinstance(payload, dict) or payload.get("enabled") is False:
            continue
        if str(payload.get("direction") or "").strip().lower() != "long":
            continue
        price_action = text(source, "price_action", "state")
        vwap_position = text(source, "vwap", "price_position")
        volume_state = text(source, "volume", "confirmation")
        breakout_confirmed = (
            price_action == "breakout"
            or (source.get("price_action") or {}).get("close_above_resistance") is True
        )
        if price_action == "bearish_push" and vwap_position == "below":
            disable(
                payload,
                "long_bearish_push_below_vwap",
                "价格行为向下且收盘位于 VWAP 下方，多单降级为等待。",
                "Bearish price action below VWAP blocks the long plan until confirmation improves.",
            )
            continue
        if volume_state == "low" and not breakout_confirmed:
            disable(
                payload,
                "long_low_volume_without_close_breakout",
                "量能偏低且没有收盘突破确认，多单降级为等待。",
                "Low volume without a confirmed close breakout blocks the long plan.",
            )

    intraday_plan = battle_plan.get("intraday_plan")
    if isinstance(intraday_plan, dict) and intraday_plan.get("enabled") is not False:
        direction = str(intraday_plan.get("direction") or "").strip().lower()
        daily_bias = text(intraday, "daily_bias")
        hourly_bias = text(intraday, "hourly_bias")
        alignment = text(intraday, "alignment")
        counter_daily = (
            (direction == "long" and daily_bias == "short")
            or (direction == "short" and daily_bias == "long")
        )
        if alignment == "hourly_only_wait_daily_confirmation" or (
            counter_daily and hourly_bias in {"neutral", "wait", ""}
        ):
            disable(
                intraday_plan,
                "countertrend_without_hourly_confirmation",
                "逆日线计划缺少小时线方向确认，当前仅允许等待。",
                "The counter-trend plan lacks hourly confirmation and is blocked until confirmation appears.",
            )

        # Counter-trend opportunities remain available only as a separate,
        # short-lived risk bucket. They cannot inherit the full-size trend
        # allocation or wait indefinitely for a stale trigger.
        countertrend = alignment in {"countertrend_long", "countertrend_short"} or counter_daily
        if countertrend and direction in {"long", "short"}:
            position_cap = min(_coerce_position_multiplier_cap(intraday_plan.get("position_multiplier_cap")), 0.5)
            intraday_plan["position_multiplier_cap"] = position_cap
            reasons = [
                str(reason)
                for reason in (intraday_plan.get("tradeability_reasons") or [])
                if str(reason).strip()
            ]
            if "countertrend_position_cap_50pct" not in reasons:
                reasons.append("countertrend_position_cap_50pct")
            intraday_plan["tradeability_reasons"] = reasons
            if intraday_plan.get("tradeability_status") not in {"blocked", "degraded_missing_context"}:
                intraday_plan["tradeability_status"] = "countertrend_limited"
            intraday_plan["countertrend_control"] = {
                "position_multiplier_cap": position_cap,
                "max_validity_bars": 6,
                "alignment": alignment or "countertrend",
            }
            contract = intraday_plan.get("execution_contract")
            entry_contract = contract.get("entry") if isinstance(contract, dict) else None
            if isinstance(entry_contract, dict):
                try:
                    entry_contract["max_wait_bars"] = min(int(entry_contract.get("max_wait_bars", 24)), 6)
                except (TypeError, ValueError):
                    entry_contract["max_wait_bars"] = 6
            ladder = intraday_plan.get("execution_ladder")
            trial_entry = ladder.get("trial_entry") if isinstance(ladder, dict) else None
            if isinstance(trial_entry, dict):
                trial_entry["position_multiplier_cap"] = position_cap

    quality_sources = {
        "derivatives": technical_context.get("derivatives"),
        "order_flow": (technical_context.get("derivatives") or {}).get("order_flow")
        if isinstance(technical_context.get("derivatives"), dict) else None,
        "macro_correlation": technical_context.get("macro_correlation"),
    }
    missing_shared = [
        key for key, value in quality_sources.items()
        if not isinstance(value, dict) or text(value, "data_quality") not in {"available", "ok", "complete"}
    ]
    for plan_key, source in (("long_plan", daily), ("short_plan", daily), ("intraday_plan", hourly or daily)):
        payload = battle_plan.get(plan_key)
        if not isinstance(payload, dict) or str(payload.get("direction") or "").strip().lower() not in {"long", "short"}:
            continue
        volatility = source.get("volatility") if isinstance(source, dict) else None
        forecast = volatility.get("forecast") if isinstance(volatility, dict) else None
        missing_layers = list(missing_shared)
        if not isinstance(forecast, dict) or text(forecast, "data_quality") not in {"available", "ok", "complete"}:
            missing_layers.append("volatility_forecast")
        if not missing_layers:
            payload.setdefault("tradeability_status", "normal")
            continue
        position_cap = min(_coerce_position_multiplier_cap(payload.get("position_multiplier_cap")), 0.5)
        payload["tradeability_status"] = "degraded_missing_context"
        existing_reasons = [
            str(reason)
            for reason in (payload.get("tradeability_reasons") or [])
            if str(reason).strip()
        ]
        payload["tradeability_reasons"] = existing_reasons + [
            f"missing:{layer}"
            for layer in missing_layers
            if f"missing:{layer}" not in existing_reasons
        ]
        payload["position_multiplier_cap"] = position_cap
        ladder = payload.get("execution_ladder")
        trial_entry = ladder.get("trial_entry") if isinstance(ladder, dict) else None
        if isinstance(trial_entry, dict):
            trial_entry["position_multiplier_cap"] = position_cap

    for plan_key in plan_keys:
        payload = battle_plan.get(plan_key)
        audit = payload.get("tradeability_audit") if isinstance(payload, dict) else None
        if not isinstance(payload, dict) or not isinstance(audit, dict):
            continue
        status = str(payload.get("tradeability_status") or "").strip().lower()
        original = audit.get("original_plan") if isinstance(audit.get("original_plan"), dict) else {}
        original_direction = str(original.get("direction") or "").strip().lower()
        if status == "blocked":
            decision = "blocked"
        elif status == "countertrend_limited":
            decision = "limited"
        elif status == "degraded_missing_context":
            decision = "degraded"
        elif original_direction in {"long", "short"}:
            decision = "passed"
        else:
            decision = "not_applicable"
        audit["applied"] = True
        audit["decision"] = decision
        audit["reasons"] = [
            str(reason)
            for reason in (payload.get("tradeability_reasons") or [])
            if str(reason).strip()
        ]


def _apply_btc_volatility_risk_overlay(
    battle_plan: Dict[str, Any],
    *,
    technical_context: Optional[Dict[str, Any]],
    language: str,
) -> None:
    """Apply deterministic EWMA volatility caps without changing direction."""

    if not isinstance(technical_context, dict):
        return
    timeframes = technical_context.get("timeframes")
    if not isinstance(timeframes, dict):
        return

    plan_sources = (
        ("long_plan", "daily"),
        ("short_plan", "daily"),
        ("intraday_plan", "hourly"),
    )
    for plan_key, timeframe in plan_sources:
        payload = battle_plan.get(plan_key)
        source_context = timeframes.get(timeframe)
        if not isinstance(payload, dict) or not isinstance(source_context, dict):
            continue
        volatility = source_context.get("volatility")
        forecast = volatility.get("forecast") if isinstance(volatility, dict) else None
        if not isinstance(forecast, dict) or forecast.get("data_quality") != "available":
            continue

        source_position_cap = _coerce_position_multiplier_cap(
            forecast.get("position_multiplier_cap"),
        )
        previous_position_cap = _coerce_position_multiplier_cap(
            payload.get("position_multiplier_cap"),
        )
        position_cap = min(source_position_cap, previous_position_cap)
        regime = str(forecast.get("regime") or "normal").strip().lower()
        forecast_sigma = forecast.get("forecast_sigma_pct")
        overlay = {
            "version": "btc-risk-overlay-v1",
            "source_model": forecast.get("model_version") or forecast.get("model") or "ewma",
            "source_timeframe": timeframe,
            "volatility_regime": regime,
            "forecast_sigma_pct": forecast_sigma,
            "historical_percentile": forecast.get("historical_percentile"),
            "source_position_multiplier_cap": source_position_cap,
            "previous_position_multiplier_cap": previous_position_cap,
            "position_multiplier_cap": position_cap,
            "risk_action": forecast.get("risk_action") or "normal",
            "applied": source_position_cap < 1.0,
            "binding": source_position_cap <= previous_position_cap and source_position_cap < 1.0,
        }
        payload["risk_overlay"] = overlay
        payload["position_multiplier_cap"] = position_cap

        if source_position_cap >= 1.0:
            continue
        if language == "zh":
            if source_position_cap <= previous_position_cap:
                note = (
                    f"EWMA 波动处于 {regime} 区间（下一根波动预测 {forecast_sigma}%），"
                    f"系统将仓位限制为原计划的 {position_cap:.0%} 以内。"
                )
            else:
                note = (
                    f"EWMA 波动处于 {regime} 区间（下一根波动预测 {forecast_sigma}%），"
                    f"其 {source_position_cap:.0%} 上限宽于现有 {position_cap:.0%} 风控，"
                    "系统保留更严格的现有仓位上限。"
                )
        else:
            if source_position_cap <= previous_position_cap:
                note = (
                    f"EWMA volatility is {regime} (next-bar sigma {forecast_sigma}%); "
                    f"cap position size at {position_cap:.0%} of the original plan."
                )
            else:
                note = (
                    f"EWMA volatility is {regime} (next-bar sigma {forecast_sigma}%); "
                    f"its {source_position_cap:.0%} cap is looser than the existing "
                    f"{position_cap:.0%} risk cap, so the stricter cap remains in force."
                )
        overlay["note"] = note
        existing_hint = str(payload.get("position_hint") or "").strip()
        if note not in existing_hint:
            payload["position_hint"] = f"{existing_hint}；{note}" if existing_hint and language == "zh" else (
                f"{existing_hint}; {note}" if existing_hint else note
            )

        ladder = payload.get("execution_ladder")
        trial_entry = ladder.get("trial_entry") if isinstance(ladder, dict) else None
        if isinstance(trial_entry, dict):
            trial_entry["position_multiplier_cap"] = position_cap
            trial_hint = str(trial_entry.get("position_hint") or "").strip()
            if note not in trial_hint:
                trial_entry["position_hint"] = (
                    f"{trial_hint}；{note}" if trial_hint and language == "zh" else
                    f"{trial_hint}; {note}" if trial_hint else note
                )


def _apply_btc_intraday_alignment_guard(
    battle_plan: Dict[str, Any],
    *,
    technical_context: Optional[Dict[str, Any]],
    language: str,
) -> None:
    """Reject only intraday plans opposed by both closed-bar timeframes.

    A counter-trend plan remains valid when the hourly timeframe actually
    supports it. This guard only handles the stronger case where daily and
    hourly bias agree against the suggested intraday direction.
    """

    if not isinstance(technical_context, dict):
        return
    intraday_context = technical_context.get("intraday")
    if not isinstance(intraday_context, dict):
        return
    alignment = str(intraday_context.get("alignment") or "").strip().lower()
    if alignment not in {"aligned_long", "aligned_short"}:
        return

    intraday_plan = battle_plan.get("intraday_plan")
    if not isinstance(intraday_plan, dict) or intraday_plan.get("enabled") is False:
        return
    direction = str(intraday_plan.get("direction") or "").strip().lower()
    opposed_direction = "short" if alignment == "aligned_long" else "long"
    if direction != opposed_direction:
        return

    dominant_direction = "多" if alignment == "aligned_long" else "空"
    direction_text = "空" if direction == "short" else "多"
    reason = (
        f"日线与小时线均偏{dominant_direction}，当前缺少支持日内{direction_text}单的闭合 K 线证据；等待小时线方向确认后再评估。"
        if language == "zh"
        else (
            "Daily and hourly closed-bar signals both oppose this intraday "
            "direction; wait for hourly confirmation before reassessing."
        )
    )
    intraday_plan["enabled"] = False
    intraday_plan["direction"] = "wait"
    intraday_plan["no_trade_reason"] = reason
    intraday_plan["reason"] = reason
    intraday_plan["multi_timeframe_alignment"] = alignment
    intraday_plan["direction_guard"] = "blocked_by_aligned_timeframes"
    ladder = intraday_plan.get("execution_ladder")
    if isinstance(ladder, dict):
        ladder["current_action"] = "wait"
        trial_entry = ladder.get("trial_entry")
        if isinstance(trial_entry, dict):
            trial_entry["enabled"] = False


def _apply_btc_trigger_execution_guard(
    battle_plan: Dict[str, Any],
    *,
    trigger_context: Optional[Dict[str, Any]],
    language: str,
) -> None:
    """Prevent a late volatility trigger from becoming an immediate chase plan."""
    if not isinstance(trigger_context, dict):
        return
    if trigger_context.get("entry_executable_now") not in {0, False, "0", "false", "False"}:
        return

    stage = str(trigger_context.get("impulse_stage") or "").strip().lower()
    if stage not in {"late_extension", "exhaustion_candidate"}:
        return

    intraday_plan = battle_plan.get("intraday_plan")
    if not isinstance(intraday_plan, dict):
        return

    current_price = trigger_context.get("price", "N/A")
    no_chase_price = trigger_context.get("no_chase_price", "N/A")
    if stage == "late_extension":
        reason_zh = (
            f"短窗口脉冲已过度延伸（当前价 {current_price}，追价上限/下限 {no_chase_price}），"
            "当前不具备可执行试仓条件；仅保留新的回踩或结构确认后再评估。"
        )
        reason_en = (
            f"The short-window impulse is overextended (current {current_price}, no-chase level {no_chase_price}); "
            "do not open a new intraday position until a new retest or structure confirmation."
        )
    else:
        reason_zh = "短窗口脉冲已衰竭，当前不具备可执行试仓条件；等待新的关键位确认。"
        reason_en = "The short-window impulse has exhausted; wait for a new key-level confirmation before any intraday entry."

    reason = reason_zh if language == "zh" else reason_en
    intraday_plan["enabled"] = False
    intraday_plan["direction"] = "wait"
    intraday_plan["no_trade_reason"] = reason
    intraday_plan["reason"] = reason
    intraday_plan["trigger_execution_state"] = stage
    ladder = intraday_plan.get("execution_ladder")
    if isinstance(ladder, dict):
        ladder["current_action"] = "wait"
        trial_entry = ladder.get("trial_entry")
        if isinstance(trial_entry, dict):
            trial_entry["enabled"] = False


class GeminiAnalyzer:
    """
    Gemini AI 分析器

    职责：
    1. 调用 Google Gemini API 进行股票分析
    2. 结合预先搜索的新闻和技术面数据生成分析报告
    3. 解析 AI 返回的 JSON 格式结果

    使用方式：
        analyzer = GeminiAnalyzer()
        result = analyzer.analyze(context, news_context)
    """

    # ========================================
    # 系统提示词 - 决策仪表盘 v2.0
    # ========================================
    # 输出格式升级：从简单信号升级为决策仪表盘
    # 核心模块：核心结论 + 数据透视 + 舆情情报 + 作战计划
    # ========================================

    LEGACY_DEFAULT_SYSTEM_PROMPT = """你是一位专注于趋势交易的{market_placeholder}投资分析师，负责生成专业的【决策仪表盘】分析报告。

{guidelines_placeholder}

""" + CORE_TRADING_SKILL_POLICY_ZH + """

## 输出格式：决策仪表盘 JSON

请严格按照以下 JSON 格式输出，这是一个完整的【决策仪表盘】：

```json
{
    "stock_name": "股票中文名称",
    "sentiment_score": 0-100整数,
    "trend_prediction": "强烈看多/看多/震荡/看空/强烈看空",
    "operation_advice": "买入/加仓/持有/减仓/卖出/观望",
    "decision_type": "buy/hold/sell",
    "confidence_level": "高/中/低",

    "dashboard": {
        "core_conclusion": {
            "one_sentence": "一句话核心结论（30字以内，直接告诉用户做什么）",
            "signal_type": "🟢买入信号/🟡持有观望/🔴卖出信号/⚠️风险警告",
            "time_sensitivity": "立即行动/今日内/本周内/不急",
            "position_advice": {
                "no_position": "空仓者建议：具体操作指引",
                "has_position": "持仓者建议：具体操作指引"
            }
        },

        "data_perspective": {
            "trend_status": {
                "ma_alignment": "均线排列状态描述",
                "is_bullish": true/false,
                "trend_score": 0-100
            },
            "price_position": {
                "current_price": 当前价格数值,
                "ma5": MA5数值,
                "ma10": MA10数值,
                "ma20": MA20数值,
                "bias_ma5": 乖离率百分比数值,
                "bias_status": "安全/警戒/危险",
                "support_level": 支撑位价格,
                "resistance_level": 压力位价格
            },
            "volume_analysis": {
                "volume_ratio": 量比数值,
                "volume_status": "放量/缩量/平量",
                "turnover_rate": 换手率百分比,
                "volume_meaning": "量能含义解读（如：缩量回调表示抛压减轻）"
            },
            "chip_structure": {
                "profit_ratio": 获利比例,
                "avg_cost": 平均成本,
                "concentration": 筹码集中度,
                "chip_health": "健康/一般/警惕"
            }
        },

        "intelligence": {
            "latest_news": "【最新消息】近期重要新闻摘要",
            "risk_alerts": ["风险点1：具体描述", "风险点2：具体描述"],
            "positive_catalysts": ["利好1：具体描述", "利好2：具体描述"],
            "earnings_outlook": "业绩预期分析（基于年报预告、业绩快报等）",
            "sentiment_summary": "舆情情绪一句话总结"
        },

        "battle_plan": {
            "sniper_points": {
                "ideal_buy": "理想买入点：XX元（在MA5附近）",
                "secondary_buy": "次优买入点：XX元（在MA10附近）",
                "stop_loss": "止损位：XX元（跌破MA20或X%）",
                "take_profit": "目标位：XX元（前高/整数关口）"
            },
            "position_strategy": {
                "suggested_position": "建议仓位：X成",
                "entry_plan": "分批建仓策略描述",
                "risk_control": "风控策略描述"
            },
            "action_checklist": [
                "✅/⚠️/❌ 检查项1：多头排列",
                "✅/⚠️/❌ 检查项2：乖离率合理（强势趋势可放宽）",
                "✅/⚠️/❌ 检查项3：量能配合",
                "✅/⚠️/❌ 检查项4：无重大利空",
                "✅/⚠️/❌ 检查项5：筹码健康",
                "✅/⚠️/❌ 检查项6：PE估值合理"
            ]
        },

        "phase_decision": {
            "phase_context": {"phase": "premarket/intraday/lunch_break/closing_auction/postmarket/non_trading/unknown"},
            "action_window": "盘前计划/盘中跟踪/午间确认/收盘前风控/盘后复盘/非交易日观察",
            "immediate_action": "立即行动/等待确认/观察/止损止盈预警/禁止追高/无盘中动作",
            "watch_conditions": ["观察条件1", "观察条件2"],
            "next_check_time": "下一次检查点或市场本地时间",
            "confidence_reason": "置信度理由，说明阶段和数据质量限制",
            "data_limitations": ["阶段或数据质量限制1", "阶段或数据质量限制2"]
        }
    },

    "analysis_summary": "100字综合分析摘要",
    "key_points": "3-5个核心看点，逗号分隔",
    "risk_warning": "风险提示",
    "buy_reason": "操作理由，引用交易理念",

    "trend_analysis": "走势形态分析",
    "short_term_outlook": "短期1-3日展望",
    "medium_term_outlook": "中期1-2周展望",
    "technical_analysis": "技术面综合分析",
    "ma_analysis": "均线系统分析",
    "volume_analysis": "量能分析",
    "pattern_analysis": "K线形态分析",
    "fundamental_analysis": "基本面分析",
    "sector_position": "板块行业分析",
    "company_highlights": "公司亮点/风险",
    "news_summary": "新闻摘要",
    "market_sentiment": "市场情绪",
    "hot_topics": "相关热点",

    "search_performed": true/false,
    "data_sources": "数据来源说明"
}
```

## 评分标准

### 强烈买入（80-100分）：
- ✅ 多头排列：MA5 > MA10 > MA20
- ✅ 低乖离率：<2%，最佳买点
- ✅ 缩量回调或放量突破
- ✅ 筹码集中健康
- ✅ 消息面有利好催化

### 买入（60-79分）：
- ✅ 多头排列或弱势多头
- ✅ 乖离率 <5%
- ✅ 量能正常
- ⚪ 允许一项次要条件不满足

### 观望（40-59分）：
- ⚠️ 乖离率 >5%（追高风险）
- ⚠️ 均线缠绕趋势不明
- ⚠️ 有风险事件

### 卖出/减仓（0-39分）：
- ❌ 空头排列
- ❌ 跌破MA20
- ❌ 放量下跌
- ❌ 重大利空

## 决策仪表盘核心原则

1. **核心结论先行**：一句话说清该买该卖
2. **分持仓建议**：空仓者和持仓者给不同建议
3. **精确狙击点**：必须给出具体价格，不说模糊的话
4. **检查清单可视化**：用 ✅⚠️❌ 明确显示每项检查结果
5. **风险优先级**：舆情中的风险点要醒目标出

## 可操作性与稳定性约束

- 不得仅因为单日涨跌或评分跨线就在“买入/卖出”之间剧烈切换。
- 操作建议必须同时参考价格位置（支撑/压力位）、量能/筹码、主力资金流向和风险事件。
- 股价位于支撑与压力之间、资金流不明确时，优先输出“持有/震荡/观望/洗盘观察”等可执行的中性建议；`decision_type` 仍保持 `hold`。
- 只有在接近支撑确认或有效突破压力，且资金流/量价配合时，才能给出买入；接近压力且资金流出时不得追买。
- 只有在跌破关键支撑、主力资金持续流出或风险显著放大时，才能给出卖出/减仓。
- 报告必须去重：同一事实或同一风险不要在摘要、技术面、新闻、检查清单中反复改写；每个字段只保留会改变交易动作、触发条件、失效条件或仓位控制的信息。
- 如果暂时没有交易机会，不要用长篇泛泛分析填充；必须直接写清“等待什么价格/量能/结构触发”以及“什么条件使该机会失效”。
- 必须输出 `dashboard.phase_decision` 七字段；盘中/午休/临近收盘要给出当前动作、观察条件和下一次检查点。
- 盘前、非交易日或未知阶段不得伪造今日盘中走势；quote/daily_bars/technical 存在 stale、fallback、missing、fetch_failed、partial 或 estimated 时，`confidence_level` 不得为高。"""

    SYSTEM_PROMPT = """你是一位{market_placeholder}投资分析师，负责生成专业的【决策仪表盘】分析报告。

{guidelines_placeholder}

{default_skill_policy_section}
{skills_section}

## 输出格式：决策仪表盘 JSON

请严格按照以下 JSON 格式输出，这是一个完整的【决策仪表盘】：

```json
{
    "stock_name": "股票中文名称",
    "sentiment_score": 0-100整数,
    "trend_prediction": "强烈看多/看多/震荡/看空/强烈看空",
    "operation_advice": "买入/加仓/持有/减仓/卖出/观望",
    "decision_type": "buy/hold/sell",
    "confidence_level": "高/中/低",

    "dashboard": {
        "core_conclusion": {
            "one_sentence": "一句话核心结论（30字以内，直接告诉用户做什么）",
            "signal_type": "🟢买入信号/🟡持有观望/🔴卖出信号/⚠️风险警告",
            "time_sensitivity": "立即行动/今日内/本周内/不急",
            "position_advice": {
                "no_position": "空仓者建议：具体操作指引",
                "has_position": "持仓者建议：具体操作指引"
            }
        },

        "data_perspective": {
            "trend_status": {
                "ma_alignment": "均线排列状态描述",
                "is_bullish": true/false,
                "trend_score": 0-100
            },
            "price_position": {
                "current_price": 当前价格数值,
                "ma5": MA5数值,
                "ma10": MA10数值,
                "ma20": MA20数值,
                "bias_ma5": 乖离率百分比数值,
                "bias_status": "安全/警戒/危险",
                "support_level": 支撑位价格,
                "resistance_level": 压力位价格
            },
            "volume_analysis": {
                "volume_ratio": 量比数值,
                "volume_status": "放量/缩量/平量",
                "turnover_rate": 换手率百分比,
                "volume_meaning": "量能含义解读（如：缩量回调表示抛压减轻）"
            },
            "chip_structure": {
                "profit_ratio": 获利比例,
                "avg_cost": 平均成本,
                "concentration": 筹码集中度,
                "chip_health": "健康/一般/警惕"
            }
        },

        "intelligence": {
            "latest_news": "【最新消息】近期重要新闻摘要",
            "risk_alerts": ["风险点1：具体描述", "风险点2：具体描述"],
            "positive_catalysts": ["利好1：具体描述", "利好2：具体描述"],
            "earnings_outlook": "业绩预期分析（基于年报预告、业绩快报等）",
            "sentiment_summary": "舆情情绪一句话总结"
        },

        "battle_plan": {
            "sniper_points": {
                "ideal_buy": "理想入场位：XX元（满足主要技能触发条件）",
                "secondary_buy": "次优入场位：XX元（更保守或确认后执行）",
                "stop_loss": "止损位：XX元（失效条件或X%风险）",
                "take_profit": "目标位：XX元（按阻力位/风险回报比制定）"
            },
            "long_plan": {
                "plan_type": "daily_long",
                "direction": "long",
                "entry_zone": "多单入场区间或触发价：XX-XX；若只给触发价也必须明确",
                "entry_price": 多单入场价数值,
                "stop_loss": 多单止损价数值,
                "take_profit": 多单目标价数值,
                "execution_ladder": {
                    "scenario": "trend_pullback/liquidity_sweep_reversal/breakout_continuation/range_rejection/wait",
                    "current_action": "trial/wait",
                    "trial_entry": {
                        "enabled": true,
                        "entry_zone": "试仓价格区间",
                        "entry_price": 试仓价数值,
                        "trigger_condition": "试仓触发条件",
                        "position_hint": "试仓仓位或账户风险预算"
                    },
                    "confirmation_add": {
                        "enabled": true,
                        "entry_price": 确认加仓价数值,
                        "trigger_condition": "确认加仓条件",
                        "position_hint": "确认后追加仓位或总风险预算"
                    },
                    "invalidation": {
                        "price": 失效价数值,
                        "condition": "失效条件",
                        "action": "触发后撤销试仓或退出"
                    }
                },
                "execution_contract": {
                    "version": "btc-execution-v1",
                    "instrument": {
                        "type": "{crypto_execution_instrument_type}",
                        "venue": "{crypto_execution_venue}",
                        "symbol": "{crypto_execution_canonical_symbol}",
                        "market_symbol": "{crypto_execution_market_symbol}",
                        "trigger_price_type": "trade",
                        "fill_price_type": "trade",
                        "liquidation_price_type": "{crypto_execution_liquidation_price_type}",
                        "margin_mode": "{crypto_execution_margin_mode}"
                    },
                    "entry": {
                        "setup_type": "breakout 或 pullback（按当前结构二选一：突破跟随选 breakout，等回踩承接选 pullback）",
                        "logic": "all",
                        "conditions": [
                            {"type": "breakout 用 close_above；pullback 用 low_lte（回踩价位）并搭配 close_above 收盘确认", "value": 多单确认价数值（应贴近 entry_price，偏离不超过0.5%）},
                            {"type": "volume_ratio_gte（breakout 必须带量能确认；pullback 可选）", "value": 最低量比数值},
                            {"type": "close_above_vwap（可选，强势结构时追加）"}
                        ],
                        "confirmation_bars": 1,
                        "fill": "next_bar_open",
                        "max_wait_bars": 3
                    },
                    "exit": {"max_holding_bars": 5}
                },
                "trigger_condition": "多单触发条件（日线计划按日线K线计：等待窗口最多3个交易日，预期持仓最长5个交易日，请按触发难易在 2-5 / 3-7 区间内取值）",
                "invalidation": "多单失效条件",
                "invalid_condition": "多单失效条件（结构化字段，和 invalidation 保持一致或更精确）",
                "risk_reward": "风险收益比：例如 1:2.0；无法计算时写明原因",
                "position_hint": "仓位建议或单笔风险预算：例如 0.5-1.0% 账户风险",
                "confidence": "置信度：高/中/低 + 原因",
                "no_trade_reason": "若暂不做多，必须写清等待原因；可交易时为空",
                "reason": "多单依据：价格行为/量能/VWAP/EMA/斐波那契共振"
            },
            "short_plan": {
                "plan_type": "daily_short",
                "direction": "short",
                "entry_zone": "空单入场区间或触发价：XX-XX；若只给触发价也必须明确",
                "entry_price": 空单入场价数值,
                "stop_loss": 空单止损价数值,
                "take_profit": 空单目标价数值,
                "execution_ladder": {
                    "scenario": "trend_pullback/liquidity_sweep_reversal/breakout_continuation/range_rejection/wait",
                    "current_action": "trial/wait",
                    "trial_entry": {
                        "enabled": true,
                        "entry_zone": "试仓价格区间",
                        "entry_price": 试仓价数值,
                        "trigger_condition": "试仓触发条件",
                        "position_hint": "试仓仓位或账户风险预算"
                    },
                    "confirmation_add": {
                        "enabled": true,
                        "entry_price": 确认加仓价数值,
                        "trigger_condition": "确认加仓条件",
                        "position_hint": "确认后追加仓位或总风险预算"
                    },
                    "invalidation": {
                        "price": 失效价数值,
                        "condition": "失效条件",
                        "action": "触发后撤销试仓或退出"
                    }
                },
                "execution_contract": {
                    "version": "btc-execution-v1",
                    "instrument": {
                        "type": "{crypto_execution_instrument_type}",
                        "venue": "{crypto_execution_venue}",
                        "symbol": "{crypto_execution_canonical_symbol}",
                        "market_symbol": "{crypto_execution_market_symbol}",
                        "trigger_price_type": "trade",
                        "fill_price_type": "trade",
                        "liquidation_price_type": "{crypto_execution_liquidation_price_type}",
                        "margin_mode": "{crypto_execution_margin_mode}"
                    },
                    "entry": {
                        "setup_type": "breakout 或 pullback（按当前结构二选一：跌破跟随选 breakout，等反抽拒绝选 pullback）",
                        "logic": "all",
                        "conditions": [
                            {"type": "breakout 用 close_below；pullback 用 high_gte（反抽价位）并搭配 close_below 收盘确认", "value": 空单确认价数值（应贴近 entry_price，偏离不超过0.5%）},
                            {"type": "volume_ratio_gte（breakout 必须带量能确认；pullback 可选）", "value": 最低量比数值},
                            {"type": "close_below_vwap（可选，弱势结构时追加）"}
                        ],
                        "confirmation_bars": 1,
                        "fill": "next_bar_open",
                        "max_wait_bars": 3
                    },
                    "exit": {"max_holding_bars": 5}
                },
                "trigger_condition": "空单触发条件（日线计划按日线K线计：等待窗口最多3个交易日，预期持仓最长5个交易日，请按触发难易在 2-5 / 3-7 区间内取值）",
                "invalidation": "空单失效条件",
                "invalid_condition": "空单失效条件（结构化字段，和 invalidation 保持一致或更精确）",
                "risk_reward": "风险收益比：例如 1:2.0；无法计算时写明原因",
                "position_hint": "仓位建议或单笔风险预算：例如 0.5-1.0% 账户风险",
                "confidence": "置信度：高/中/低 + 原因",
                "no_trade_reason": "若暂不做空，必须写清等待原因；可交易时为空",
                "reason": "空单依据：价格行为/量能/VWAP/EMA/斐波那契共振"
            },
            "intraday_plan": {
                "plan_type": "intraday",
                "enabled": false,
                "direction": "none/long/short/wait",
                "entry_zone": "小时线日内入场区间或触发价：XX-XX；无机会则写等待",
                "entry_price": 小时线日内入场价数值或null,
                "stop_loss": 小时线日内止损价数值或null,
                "take_profit": 小时线日内目标价数值或null,
                "execution_ladder": {
                    "scenario": "trend_pullback/liquidity_sweep_reversal/breakout_continuation/range_rejection/wait",
                    "current_action": "trial/wait",
                    "trial_entry": {
                        "enabled": true/false,
                        "entry_zone": "试仓价格区间或等待",
                        "entry_price": 试仓价数值或null,
                        "trigger_condition": "试仓触发条件",
                        "position_hint": "试仓仓位或账户风险预算"
                    },
                    "confirmation_add": {
                        "enabled": true/false,
                        "entry_price": 确认加仓价数值或null,
                        "trigger_condition": "确认加仓条件",
                        "position_hint": "确认后追加仓位或总风险预算"
                    },
                    "invalidation": {
                        "price": 失效价数值或null,
                        "condition": "失效条件",
                        "action": "触发后撤销试仓或退出"
                    }
                },
                "execution_contract": {
                    "version": "btc-execution-v1",
                    "instrument": {
                        "type": "{crypto_execution_instrument_type}",
                        "venue": "{crypto_execution_venue}",
                        "symbol": "{crypto_execution_canonical_symbol}",
                        "market_symbol": "{crypto_execution_market_symbol}",
                        "trigger_price_type": "trade",
                        "fill_price_type": "trade",
                        "liquidation_price_type": "{crypto_execution_liquidation_price_type}",
                        "margin_mode": "{crypto_execution_margin_mode}"
                    },
                    "entry": {
                        "setup_type": "breakout 或 pullback（按小时线结构二选一）",
                        "logic": "all",
                        "conditions": [
                            {"type": "breakout 用 close_above/close_below；pullback 用 low_lte/high_gte 触碰并搭配收盘确认", "value": 日内确认价数值（应贴近 entry_price，偏离不超过0.5%）},
                            {"type": "volume_ratio_gte（breakout 必须带量能确认；pullback 可选）", "value": 最低量比数值},
                            {"type": "close_above_vwap/close_below_vwap（可选，按强弱结构追加）"}
                        ],
                        "confirmation_bars": 1,
                        "fill": "next_bar_open",
                        "max_wait_bars": 8
                    },
                    "exit": {"max_holding_bars": 12}
                },
                "trigger_condition": "小时线触发条件（日内计划按小时线K线计：等待窗口最长8小时，预期持仓最长12小时）",
                "invalidation": "小时线失效条件",
                "invalid_condition": "小时线失效条件（结构化字段，和 invalidation 保持一致或更精确）",
                "daily_constraint": "日线方向/关键支撑阻力/失效条件如何约束本次日内交易",
                "risk_reward": "风险收益比：例如 1:1.5；无法计算时写明原因",
                "position_hint": "仓位建议或单笔风险预算，日内逆日线时必须更轻",
                "confidence": "置信度：高/中/低 + 原因",
                "no_trade_reason": "enabled=false 或 direction=wait 时必须写清不交易原因",
                "reason": "小时线日内机会依据；无机会时说明原因"
            },
            "position_strategy": {
                "suggested_position": "建议仓位：X成",
                "entry_plan": "分批建仓策略描述",
                "risk_control": "风控策略描述"
            },
            "action_checklist": [
                "✅/⚠️/❌ 检查项1：当前结构是否满足激活技能条件",
                "✅/⚠️/❌ 检查项2：入场位置与风险回报是否合理",
                "✅/⚠️/❌ 检查项3：量价/波动/筹码是否支持判断",
                "✅/⚠️/❌ 检查项4：无重大利空",
                "✅/⚠️/❌ 检查项5：仓位与止损计划明确",
                "✅/⚠️/❌ 检查项6：估值/业绩/催化与结论匹配"
            ]
        },

        "phase_decision": {
            "phase_context": {"phase": "premarket/intraday/lunch_break/closing_auction/postmarket/non_trading/unknown"},
            "action_window": "盘前计划/盘中跟踪/午间确认/收盘前风控/盘后复盘/非交易日观察",
            "immediate_action": "立即行动/等待确认/观察/止损止盈预警/禁止追高/无盘中动作",
            "watch_conditions": ["观察条件1", "观察条件2"],
            "next_check_time": "下一次检查点或市场本地时间",
            "confidence_reason": "置信度理由，说明阶段和数据质量限制",
            "data_limitations": ["阶段或数据质量限制1", "阶段或数据质量限制2"]
        }
    },

    "analysis_summary": "100字综合分析摘要",
    "key_points": "3-5个核心看点，逗号分隔",
    "risk_warning": "风险提示",
    "buy_reason": "操作理由，引用激活技能或风险框架",

    "trend_analysis": "走势形态分析",
    "short_term_outlook": "短期1-3日展望",
    "medium_term_outlook": "中期1-2周展望",
    "technical_analysis": "技术面综合分析",
    "ma_analysis": "均线系统分析",
    "volume_analysis": "量能分析",
    "pattern_analysis": "K线形态分析",
    "fundamental_analysis": "基本面分析",
    "sector_position": "板块行业分析",
    "company_highlights": "公司亮点/风险",
    "news_summary": "新闻摘要",
    "market_sentiment": "市场情绪",
    "hot_topics": "相关热点",

    "search_performed": true/false,
    "data_sources": "数据来源说明"
}
```

## 评分标准

### 强烈买入（80-100分）：
- ✅ 多个激活技能同时支持积极结论
- ✅ 上行空间、触发条件与风险回报清晰
- ✅ 关键风险已排查，仓位与止损计划明确
- ✅ 重要数据和情报结论彼此一致

### 买入（60-79分）：
- ✅ 主信号偏积极，但仍有少量待确认项
- ✅ 允许存在可控风险或次优入场点
- ✅ 需要在报告中明确补充观察条件

### 观望（40-59分）：
- ⚠️ 信号分歧较大，或缺乏足够确认
- ⚠️ 风险与机会大致均衡
- ⚠️ 更适合等待触发条件或回避不确定性

### 卖出/减仓（0-39分）：
- ❌ 主要结论转弱，风险明显高于收益
- ❌ 触发了止损/失效条件或重大利空
- ❌ 现有仓位更需要保护而不是进攻

## 决策仪表盘核心原则

1. **核心结论先行**：一句话说清该买该卖
2. **分持仓建议**：空仓者和持仓者给不同建议
3. **精确狙击点**：必须给出具体价格，不说模糊的话
4. **检查清单可视化**：用 ✅⚠️❌ 明确显示每项检查结果
5. **风险优先级**：舆情中的风险点要醒目标出

## 可操作性与稳定性约束

- 不得仅因为单日涨跌或评分跨线就在“买入/卖出”之间剧烈切换。
- 操作建议必须同时参考价格位置（支撑/压力位）、量能/筹码、主力资金流向和风险事件。
- 股价位于支撑与压力之间、资金流不明确时，优先输出“持有/震荡/观望/洗盘观察”等可执行的中性建议；`decision_type` 仍保持 `hold`。
- 只有在接近支撑确认或有效突破压力，且资金流/量价配合时，才能给出买入；接近压力且资金流出时不得追买。
- 只有在跌破关键支撑、主力资金持续流出或风险显著放大时，才能给出卖出/减仓。
- 必须输出 `dashboard.phase_decision` 七字段；盘中/午休/临近收盘要给出当前动作、观察条件和下一次检查点。
- 盘前、非交易日或未知阶段不得伪造今日盘中走势；quote/daily_bars/technical 存在 stale、fallback、missing、fetch_failed、partial 或 estimated 时，`confidence_level` 不得为高。"""

    TEXT_SYSTEM_PROMPT = """你是一位专业的股票分析助手。

- 回答必须基于用户提供的数据与上下文
- 若信息不足，要明确指出不确定性
- 不要编造价格、财报或新闻事实
"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        config: Optional[Config] = None,
        skills: Optional[List[str]] = None,
        skill_instructions: Optional[str] = None,
        default_skill_policy: Optional[str] = None,
        use_legacy_default_prompt: Optional[bool] = None,
    ):
        """Initialize LLM Analyzer via LiteLLM.

        Args:
            api_key: Ignored (kept for backward compatibility). Keys are loaded from config.
        """
        self._config_override = config
        self._requested_skills = list(skills) if skills is not None else None
        self._skill_instructions_override = skill_instructions
        self._default_skill_policy_override = default_skill_policy
        self._use_legacy_default_prompt_override = use_legacy_default_prompt
        self._resolved_prompt_state: Optional[Dict[str, Any]] = None
        self._router = None
        self._legacy_router_model_list: List[Dict[str, Any]] = []
        self._litellm_available = False
        self._init_litellm()
        if not self._litellm_available:
            logger.warning("No LLM configured (LITELLM_MODEL / API keys), AI analysis will be unavailable")

    def _get_runtime_config(self) -> Config:
        """Return the runtime config, honoring injected overrides for tests/pipeline."""
        return getattr(self, "_config_override", None) or get_config()

    def _get_skill_prompt_sections(self) -> tuple[str, str, bool]:
        """Resolve skill instructions + default baseline + prompt mode."""
        skill_instructions = getattr(self, "_skill_instructions_override", None)
        default_skill_policy = getattr(self, "_default_skill_policy_override", None)
        use_legacy_default_prompt = getattr(self, "_use_legacy_default_prompt_override", None)

        if skill_instructions is not None and default_skill_policy is not None:
            return (
                skill_instructions,
                default_skill_policy,
                bool(use_legacy_default_prompt) if use_legacy_default_prompt is not None else False,
            )

        resolved_state = getattr(self, "_resolved_prompt_state", None)
        if resolved_state is None:
            from src.agent.factory import resolve_skill_prompt_state

            prompt_state = resolve_skill_prompt_state(
                self._get_runtime_config(),
                skills=getattr(self, "_requested_skills", None),
            )
            resolved_state = {
                "skill_instructions": prompt_state.skill_instructions,
                "default_skill_policy": prompt_state.default_skill_policy,
                "use_legacy_default_prompt": bool(getattr(prompt_state, "use_legacy_default_prompt", False)),
            }
            self._resolved_prompt_state = resolved_state

        return (
            skill_instructions if skill_instructions is not None else resolved_state.get("skill_instructions", ""),
            default_skill_policy if default_skill_policy is not None else resolved_state.get("default_skill_policy", ""),
            (
                use_legacy_default_prompt
                if use_legacy_default_prompt is not None
                else bool(resolved_state.get("use_legacy_default_prompt", False))
            ),
        )

    def _adapt_default_skill_policy_for_context(
        self,
        default_skill_policy: str,
        *,
        context: Dict[str, Any],
        use_legacy_default_prompt: bool,
    ) -> str:
        """Use a two-way BTC baseline instead of the stock long-only baseline."""

        if not default_skill_policy or not use_legacy_default_prompt:
            return default_skill_policy

        market = str(context.get("market") or "").strip().lower()
        code = str(context.get("code") or context.get("stock_code") or "").strip()
        normalized = normalize_stock_code(code)
        if market != "crypto" and get_market_for_stock(normalized) != "crypto":
            return default_skill_policy

        from src.agent.skills.defaults import get_crypto_two_way_trading_skill_policy

        return get_crypto_two_way_trading_skill_policy(explicit_skill_selection=False)

    def _get_analysis_system_prompt(self, report_language: str, stock_code: str = "") -> str:
        """Build the analyzer system prompt with output-language guidance."""
        lang = normalize_report_language(report_language)
        market_role = get_market_role(stock_code, lang)
        market_guidelines = get_market_guidelines(stock_code, lang)
        skill_instructions, default_skill_policy, use_legacy_default_prompt = self._get_skill_prompt_sections()
        default_skill_policy = self._adapt_default_skill_policy_for_context(
            default_skill_policy,
            context={"code": stock_code},
            use_legacy_default_prompt=use_legacy_default_prompt,
        )
        if use_legacy_default_prompt:
            if default_skill_policy and default_skill_policy != CORE_TRADING_SKILL_POLICY_ZH:
                base_prompt = (
                    self.SYSTEM_PROMPT.replace("{market_placeholder}", market_role)
                    .replace("{guidelines_placeholder}", market_guidelines)
                    .replace("{default_skill_policy_section}", f"{default_skill_policy}\n")
                    .replace("{skills_section}", "")
                )
            else:
                base_prompt = self.LEGACY_DEFAULT_SYSTEM_PROMPT.replace(
                    "{market_placeholder}", market_role
                ).replace(
                    "{guidelines_placeholder}", market_guidelines
            )
        else:
            skills_section = ""
            if skill_instructions:
                skills_section = f"## 激活的交易技能\n\n{skill_instructions}\n"
            default_skill_policy_section = ""
            if default_skill_policy:
                default_skill_policy_section = f"{default_skill_policy}\n"
            base_prompt = (
                self.SYSTEM_PROMPT.replace("{market_placeholder}", market_role)
                .replace("{guidelines_placeholder}", market_guidelines)
                .replace("{default_skill_policy_section}", default_skill_policy_section)
                .replace("{skills_section}", skills_section)
            )
        runtime_config = self._get_runtime_config()
        execution_venue = str(
            getattr(runtime_config, "crypto_trading_exchange", "okx") or "okx"
        ).strip().lower()
        execution_symbol = str(
            getattr(runtime_config, "crypto_trading_default_symbol", "BTC/USDT:USDT")
            or "BTC/USDT:USDT"
        ).strip()
        execution_margin_mode = (
            getattr(runtime_config, "okx_td_mode", "cross")
            if execution_venue == "okx"
            else getattr(runtime_config, "bybit_margin_mode", "isolated")
        )
        execution_instrument = resolve_crypto_instrument(
            execution_symbol,
            default_type="perpetual" if execution_symbol.upper().endswith(":USDT") else "spot",
            venue=execution_venue,
            margin_mode=execution_margin_mode,
        ) or resolve_crypto_instrument(
            "BTC-USDT-PERP",
            default_type="perpetual",
            venue="okx",
            margin_mode="isolated",
        )
        instrument_values = {
            "{crypto_execution_instrument_type}": execution_instrument.instrument_type,
            "{crypto_execution_venue}": execution_instrument.venue,
            "{crypto_execution_canonical_symbol}": execution_instrument.canonical_symbol,
            "{crypto_execution_market_symbol}": execution_instrument.market_symbol,
            "{crypto_execution_liquidation_price_type}": execution_instrument.liquidation_price_type or "",
            "{crypto_execution_margin_mode}": execution_instrument.margin_mode or "",
        }
        for placeholder, value in instrument_values.items():
            base_prompt = base_prompt.replace(placeholder, value)
        if lang == "en":
            return base_prompt + """

## Output Language (highest priority)

- Keep all JSON keys unchanged.
- `decision_type` must remain `buy|hold|sell`.
- All human-readable JSON values must be written in English.
- Use the common English company name when you are confident; otherwise keep the original listed company name instead of inventing one.
- This includes `stock_name`, `trend_prediction`, `operation_advice`, `confidence_level`, nested dashboard text, checklist items, and all narrative summaries.
"""
        return base_prompt + """

## 输出语言（最高优先级）

- 所有 JSON 键名保持不变。
- `decision_type` 必须保持为 `buy|hold|sell`。
- 所有面向用户的人类可读文本值必须使用中文。
"""

    def _has_channel_config(self, config: Config) -> bool:
        """Check if multi-channel config (channels / YAML / legacy model_list) is active."""
        return bool(config.llm_model_list) and not all(
            e.get('model_name', '').startswith('__legacy_') for e in config.llm_model_list
        )

    @staticmethod
    def _legacy_router_provider_alias(model: str) -> str:
        provider = model.split("/", 1)[0] if "/" in model else "openai"
        return f"__legacy_{provider}__"

    @staticmethod
    def _build_legacy_router_model_list_from_config(
        model: str,
        model_list: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Build legacy-router candidates from configured legacy llm_model_list entries."""
        if not model:
            return []
        target_model = model
        target_legacy_alias = GeminiAnalyzer._legacy_router_provider_alias(model)
        legacy_entries: List[Dict[str, Any]] = []
        for entry in model_list or []:
            if not isinstance(entry, dict):
                continue
            model_name = str(entry.get("model_name") or "").strip()
            if model_name != target_legacy_alias:
                continue

            params = entry.get("litellm_params")
            if not isinstance(params, dict):
                continue

            api_key = str(params.get("api_key") or "").strip()
            if not api_key or len(api_key) < 8:
                continue

            deployed_params = dict(params)
            deployed_params["model"] = target_model
            deployed_params["api_key"] = api_key
            legacy_entries.append({
                "model_name": target_model,
                "litellm_params": deployed_params,
            })

        return legacy_entries

    def _init_litellm(self) -> None:
        """Initialize litellm Router from channels / YAML / legacy keys."""
        config = self._get_runtime_config()
        litellm_model = config.litellm_model
        if not litellm_model:
            logger.warning("Analyzer LLM: LITELLM_MODEL not configured")
            return

        self._litellm_available = True

        # --- Channel / YAML path: build Router from pre-built model_list ---
        if self._has_channel_config(config):
            model_list = config.llm_model_list
            try:
                self._router = Router(
                    model_list=model_list,
                    routing_strategy="simple-shuffle",
                    num_retries=2,
                )
            except TypeError:
                logger.debug("Analyzer LLM: Router constructor signature not compatible; fallback to direct mode")
                self._router = None
            else:
                unique_models = list(dict.fromkeys(
                    e['litellm_params']['model'] for e in model_list
                ))
                logger.info(
                    f"Analyzer LLM: Router initialized from channels/YAML — "
                    f"{len(model_list)} deployment(s), models: {unique_models}"
                )
                return

        # --- Legacy path: build Router for multi-key, or use single key ---
        keys = get_api_keys_for_model(litellm_model, config)
        legacy_model_list = self._build_legacy_router_model_list_from_config(
            litellm_model,
            config.llm_model_list,
        )
        if len(legacy_model_list) <= 1 and keys:
            extra_params = extra_litellm_params(litellm_model, config)
            configured_model_list = [
                {
                    "model_name": litellm_model,
                    "litellm_params": {
                        "model": litellm_model,
                        "api_key": k,
                        **extra_params,
                    },
                }
                for k in keys
            ]
            if not legacy_model_list:
                legacy_model_list = configured_model_list
            elif len(legacy_model_list) < len(configured_model_list):
                legacy_model_list = configured_model_list

        if len(legacy_model_list) > 1:
            self._legacy_router_model_list = legacy_model_list
            try:
                self._router = Router(
                    model_list=legacy_model_list,
                    routing_strategy="simple-shuffle",
                    num_retries=2,
                )
            except TypeError:
                logger.debug("Analyzer LLM: Legacy Router constructor signature not compatible; using legacy model_list fallback")
                self._router = None
            else:
                logger.info(
                    f"Analyzer LLM: Legacy Router initialized with {len(legacy_model_list)} keys "
                    f"for {litellm_model}"
                )
                return

        if keys:
            logger.info(f"Analyzer LLM: litellm initialized (model={litellm_model})")
        else:
            logger.info(
                f"Analyzer LLM: litellm initialized (model={litellm_model}, "
                f"API key from environment)"
            )

    def is_available(self) -> bool:
        """Check if LiteLLM is properly configured with at least one API key."""
        return self._router is not None or self._litellm_available

    def _dispatch_litellm_completion(
        self,
        model: str,
        call_kwargs: Dict[str, Any],
        *,
        config: Config,
        use_channel_router: bool,
        router_model_names: set[str],
    ) -> Any:
        """Dispatch a LiteLLM completion through router or direct fallback."""
        wire_models = resolve_fallback_litellm_wire_models(model, config.llm_model_list)
        register_fallback_model_pricing(wire_models)
        effective_kwargs = dict(call_kwargs)
        if use_channel_router and self._router and model in router_model_names:
            return self._router.completion(**effective_kwargs)
        if self._router and model == config.litellm_model and not use_channel_router:
            return self._router.completion(**effective_kwargs)

        keys = get_api_keys_for_model(model, config)
        if keys:
            effective_kwargs["api_key"] = keys[0]
        effective_kwargs.update(extra_litellm_params(model, config))
        return litellm.completion(**effective_kwargs)

    def _normalize_usage(
        self,
        usage_obj: Any,
        *,
        model: str = "",
        provider: Optional[str] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Normalize usage objects from LiteLLM responses/chunks."""
        if not usage_obj:
            return attach_message_hmacs({}, messages) if messages is not None else {}
        usage = normalize_litellm_usage(usage_obj, model=model, provider=provider)
        if messages is not None:
            usage = attach_message_hmacs(usage, messages)
        return usage

    @staticmethod
    def _get_response_field(obj: Any, key: str) -> Any:
        """Read a field from dict-like or object-like LiteLLM payloads."""
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    def _extract_text_blocks(self, blocks: Any) -> str:
        """Extract text from OpenAI-compatible content block lists."""
        if not blocks:
            return ""

        parts: List[str] = []
        for block in blocks:
            if isinstance(block, str):
                parts.append(block)
                continue

            text = None
            if isinstance(block, dict):
                text = block.get("text")
                if text is None:
                    text = block.get("content")
            else:
                text = getattr(block, "text", None)
                if text is None:
                    text = getattr(block, "content", None)

            if isinstance(text, str) and text:
                parts.append(text)

        return "".join(parts).strip()

    def _extract_completion_text(self, response: Any) -> str:
        """Extract text from non-stream LiteLLM completion responses."""
        choices = self._get_response_field(response, "choices")
        if not choices:
            return ""

        choice = choices[0]
        message = self._get_response_field(choice, "message")

        content_blocks = self._get_response_field(choice, "content_blocks")
        if content_blocks is None and message is not None:
            content_blocks = self._get_response_field(message, "content_blocks")
        block_text = self._extract_text_blocks(content_blocks)
        if block_text:
            return block_text

        content = None
        if message is not None:
            content = self._get_response_field(message, "content")
        if content is None:
            content = self._get_response_field(choice, "content")

        if isinstance(content, list):
            return self._extract_text_blocks(content)
        if isinstance(content, str):
            return content.strip()
        return str(content).strip() if content is not None else ""

    def _extract_stream_text(self, chunk: Any) -> str:
        """Extract provider-agnostic text delta from a LiteLLM streaming chunk."""
        choices = chunk.get("choices") if isinstance(chunk, dict) else getattr(chunk, "choices", None)
        if not choices:
            return ""

        choice = choices[0]
        delta = choice.get("delta") if isinstance(choice, dict) else getattr(choice, "delta", None)
        message = choice.get("message") if isinstance(choice, dict) else getattr(choice, "message", None)

        content: Any = None
        if isinstance(delta, dict):
            content = delta.get("content")
        elif isinstance(delta, str):
            content = delta
        elif delta is not None:
            content = getattr(delta, "content", None)

        if content is None:
            if isinstance(message, dict):
                content = message.get("content")
            elif message is not None:
                content = getattr(message, "content", None)

        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts)

        return content if isinstance(content, str) else ""

    def _consume_litellm_stream(
        self,
        stream_response: Any,
        *,
        model: str,
        usage_model: Optional[str] = None,
        provider: Optional[str] = None,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Consume a LiteLLM stream into a single text payload."""
        chunks: List[str] = []
        usage: Dict[str, Any] = {}
        chars_received = 0
        next_emit_at = 1

        try:
            for chunk in stream_response:
                chunk_usage = extract_usage_payload(chunk)
                normalized_usage = self._normalize_usage(
                    chunk_usage,
                    model=usage_model or model,
                    provider=provider,
                )
                if normalized_usage:
                    usage = normalized_usage

                delta_text = self._extract_stream_text(chunk)
                if not delta_text:
                    continue

                chunks.append(delta_text)
                chars_received += len(delta_text)
                if progress_callback and chars_received >= next_emit_at:
                    progress_callback(chars_received)
                    next_emit_at = chars_received + 160
        except Exception as exc:
            raise _LiteLLMStreamError(
                f"{model} stream interrupted: {exc}",
                partial_received=chars_received > 0,
            ) from exc

        response_text = "".join(chunks).strip()
        if not response_text:
            raise _LiteLLMStreamError(
                f"{model} stream returned empty response",
                partial_received=False,
            )

        if progress_callback and chars_received > 0:
            progress_callback(chars_received)

        return response_text, usage

    def _call_litellm(
        self,
        prompt: str,
        generation_config: dict,
        *,
        system_prompt: Optional[str] = None,
        stream: bool = False,
        stream_progress_callback: Optional[Callable[[int], None]] = None,
        response_validator: Optional[Callable[[str], None]] = None,
    ) -> Tuple[str, str, Dict[str, Any]]:
        """Call LLM via litellm with fallback across configured models.

        When channels/YAML are configured, every model goes through the Router
        (which handles per-model key selection, load balancing, and retries).
        In legacy mode, the primary model may use the Router while fallback
        models fall back to direct litellm.completion().

        Args:
            prompt: User prompt text.
            generation_config: Dict with optional keys: temperature, max_output_tokens, max_tokens.
            response_validator: Optional callable that accepts the raw response text and raises
                an exception if the response is unacceptable (e.g. not valid JSON).  When it
                raises, the current model is treated as failed and the next fallback model is
                tried.  If all models fail validation, :class:`_AllModelsFailedError` is raised
                with ``last_response_text`` set to the last raw response received.

        Returns:
            Tuple of (response text, model_used, usage). On success model_used is the full model
            name and usage is a dict with prompt_tokens, completion_tokens, total_tokens.
        """
        config = self._get_runtime_config()
        max_tokens = (
            generation_config.get('max_output_tokens')
            or generation_config.get('max_tokens')
            or 8192
        )
        requested_temperature = generation_config.get('temperature', 0.7)

        models_to_try = [config.litellm_model] + (config.litellm_fallback_models or [])
        models_to_try = [m for m in models_to_try if m]

        use_channel_router = self._has_channel_config(config)

        last_error = None
        last_response_text: Optional[str] = None
        last_model: Optional[str] = None
        last_usage: Dict[str, Any] = {}
        effective_system_prompt = system_prompt or self.TEXT_SYSTEM_PROMPT
        router_model_names = set(get_configured_llm_models(config.llm_model_list))
        for model in models_to_try:
            recovery_model_list = config.llm_model_list
            legacy_router_model_list = getattr(self, "_legacy_router_model_list", None) or []
            if legacy_router_model_list and model == config.litellm_model and not use_channel_router:
                recovery_model_list = legacy_router_model_list
            usage_model, usage_provider = resolved_model_provider_identity(model, recovery_model_list)

            try:
                model_short = model.split("/")[-1] if "/" in model else model
                extra = get_thinking_extra_body(model_short)
                call_kwargs: Dict[str, Any] = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": effective_system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": max_tokens,
                }
                if extra:
                    call_kwargs["extra_body"] = extra
                uses_router = (
                    (use_channel_router and self._router and model in router_model_names)
                    or (self._router and model == config.litellm_model and not use_channel_router)
                )
                if not uses_router:
                    try:
                        keys = get_api_keys_for_model(model, config)
                    except AttributeError:
                        keys = []
                    if keys:
                        call_kwargs["api_key"] = keys[0]
                    try:
                        call_kwargs.update(extra_litellm_params(model, config))
                    except AttributeError:
                        pass
                call_kwargs = apply_litellm_generation_params(
                    call_kwargs,
                    model,
                    requested_temperature,
                    model_list=recovery_model_list,
                )

                _stream_text: Optional[str] = None
                _stream_usage: Dict[str, Any] = {}

                if stream:
                    try:
                        stream_response = call_litellm_with_param_recovery(
                            lambda kwargs: self._dispatch_litellm_completion(
                                model,
                                kwargs,
                                config=config,
                                use_channel_router=use_channel_router,
                                router_model_names=router_model_names,
                            ),
                            model=model,
                            call_kwargs={**call_kwargs, "stream": True},
                            model_list=recovery_model_list,
                            cache_recovery=False,
                            logger=logger,
                        )
                        _stream_text, _stream_usage = self._consume_litellm_stream(
                            stream_response,
                            model=model,
                            usage_model=usage_model,
                            provider=usage_provider,
                            progress_callback=stream_progress_callback,
                        )
                    except _LiteLLMStreamError as exc:
                        if exc.partial_received:
                            logger.warning(
                                "[LiteLLM] %s stream failed after partial output, retrying non-stream for same model: %s",
                                model,
                                exc,
                            )
                        else:
                            logger.warning(
                                "[LiteLLM] %s stream unavailable before first chunk, falling back to non-stream: %s",
                                model,
                                exc,
                            )
                        last_error = exc
                    except Exception as exc:
                        logger.warning(
                            "[LiteLLM] %s stream request failed before first chunk, falling back to non-stream: %s",
                            model,
                            exc,
                        )

                if _stream_text is not None:
                    last_response_text = _stream_text
                    last_model = model
                    _stream_usage = attach_message_hmacs(_stream_usage, call_kwargs["messages"])
                    last_usage = _stream_usage
                    if response_validator is not None:
                        response_validator(_stream_text)
                    return _stream_text, model, _stream_usage

                response = call_litellm_with_param_recovery(
                    lambda kwargs: self._dispatch_litellm_completion(
                        model,
                        kwargs,
                        config=config,
                        use_channel_router=use_channel_router,
                        router_model_names=router_model_names,
                    ),
                    model=model,
                    call_kwargs=call_kwargs,
                    model_list=recovery_model_list,
                    logger=logger,
                )

                content = self._extract_completion_text(response)
                if content:
                    usage = self._normalize_usage(
                        extract_usage_payload(response),
                        model=usage_model or model,
                        provider=usage_provider,
                        messages=call_kwargs["messages"],
                    )
                    last_response_text = content
                    last_model = model
                    last_usage = usage
                    if response_validator is not None:
                        response_validator(content)
                    return (content, model, usage)
                raise ValueError("LLM returned empty response")

            except Exception as e:
                logger.warning(f"[LiteLLM] {model} failed: {e}")
                last_error = e
                continue

        raise _AllModelsFailedError(
            f"All LLM models failed (tried {len(models_to_try)} model(s)). Last error: {last_error}",
            last_response_text=last_response_text,
            last_model=last_model,
            last_usage=last_usage,
        )

    def generate_text(
        self,
        prompt: str,
        max_tokens: int = 2048,
        temperature: float = 0.7,
    ) -> Optional[str]:
        """Public entry point for free-form text generation.

        External callers (e.g. MarketAnalyzer) must use this method instead of
        calling _call_litellm() directly or accessing private attributes such as
        _litellm_available, _router, _model, _use_openai, or _use_anthropic.

        Args:
            prompt:      Text prompt to send to the LLM.
            max_tokens:  Maximum tokens in the response (default 2048).
            temperature: Sampling temperature (default 0.7).

        Returns:
            Response text, or None if the LLM call fails (error is logged).
        """
        try:
            result = self._call_litellm(
                prompt,
                generation_config={"max_tokens": max_tokens, "temperature": temperature},
            )
            if isinstance(result, tuple):
                text, model_used, usage = result
                persist_llm_usage(usage, model_used, call_type="market_review")
                return text
            return result
        except Exception as exc:
            logger.error("[generate_text] LLM call failed: %s", exc)
            return None

    def analyze(
        self, 
        context: Dict[str, Any],
        news_context: Optional[str] = None,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        stream_progress_callback: Optional[Callable[[int], None]] = None,
        analysis_context_pack_summary: Optional[str] = None,
    ) -> AnalysisResult:
        """
        分析单只股票
        
        流程：
        1. 格式化输入数据（技术面 + 新闻）
        2. 调用 Gemini API（带重试和模型切换）
        3. 解析 JSON 响应
        4. 返回结构化结果
        
        Args:
            context: 从 storage.get_analysis_context() 获取的上下文数据
            news_context: 预先搜索的新闻内容（可选）
            
        Returns:
            AnalysisResult 对象
        """
        def _emit_progress(progress: int, message: str) -> None:
            if progress_callback is None:
                return
            try:
                progress_callback(progress, message)
            except Exception as exc:
                logger.debug("[analyzer] progress callback skipped: %s", exc)

        code = context.get('code', 'Unknown')
        is_crypto_context = _is_crypto_analysis_context(context)
        config = self._get_runtime_config()
        report_language = normalize_report_language(getattr(config, "report_language", "zh"))
        analysis_mode = normalize_analysis_mode(context.get("analysis_mode"))
        analysis_timeframe = analysis_timeframe_label(analysis_mode, report_language)
        system_prompt = self._get_analysis_system_prompt(report_language, stock_code=code)
        
        # 请求前增加延时（防止连续请求触发限流）
        request_delay = config.gemini_request_delay
        if request_delay > 0:
            logger.debug(f"[LLM] 请求前等待 {request_delay:.1f} 秒...")
            _emit_progress(65, f"{code}：LLM 请求前等待 {request_delay:.1f} 秒")
            time.sleep(request_delay)
        
        # 优先从上下文获取股票名称（由 main.py 传入）
        name = context.get('stock_name')
        if not name or name.startswith('股票'):
            # 备选：从 realtime 中获取
            if 'realtime' in context and context['realtime'].get('name'):
                name = context['realtime']['name']
            else:
                # 最后从映射表获取
                name = STOCK_NAME_MAP.get(code, f'股票{code}')
        
        # 如果模型不可用，返回默认结果
        if not self.is_available():
            return AnalysisResult(
                code=code,
                name=name,
                sentiment_score=50,
                trend_prediction='Sideways' if report_language == "en" else '震荡',
                operation_advice='Hold' if report_language == "en" else '持有',
                confidence_level='Low' if report_language == "en" else '低',
                analysis_summary='AI analysis is unavailable because no API key is configured.' if report_language == "en" else 'AI 分析功能未启用（未配置 API Key）',
                risk_warning='Configure an LLM API key (GEMINI_API_KEY/ANTHROPIC_API_KEY/OPENAI_API_KEY) and retry.' if report_language == "en" else '请配置 LLM API Key（GEMINI_API_KEY/ANTHROPIC_API_KEY/OPENAI_API_KEY）后重试',
                success=False,
                error_message='LLM API key is not configured' if report_language == "en" else 'LLM API Key 未配置',
                model_used=None,
                report_language=report_language,
                analysis_mode=analysis_mode,
                analysis_timeframe=analysis_timeframe,
            )
        
        try:
            # 格式化输入（包含技术面数据和新闻）
            prompt = self._format_prompt(
                context,
                name,
                news_context,
                report_language=report_language,
                analysis_context_pack_summary=analysis_context_pack_summary,
            )
            
            config = self._get_runtime_config()
            model_name = config.litellm_model or "unknown"
            logger.info(f"========== AI 分析 {name}({code}) ==========")
            logger.info(f"[LLM配置] 模型: {model_name}")
            logger.info(f"[LLM配置] Prompt 长度: {len(prompt)} 字符")
            logger.info(f"[LLM配置] 是否包含新闻: {'是' if news_context else '否'}")

            # 记录完整 prompt 到日志（INFO级别记录摘要，DEBUG记录完整）
            prompt_preview = prompt[:500] + "..." if len(prompt) > 500 else prompt
            logger.info(f"[LLM Prompt 预览]\n{prompt_preview}")
            logger.debug(f"=== 完整 Prompt ({len(prompt)}字符) ===\n{prompt}\n=== End Prompt ===")

            # 设置生成配置
            generation_config = {
                "temperature": config.llm_temperature,
                "max_output_tokens": 8192,
            }

            logger.info(f"[LLM调用] 开始调用 {model_name}...")
            _emit_progress(68, f"{name}：LLM 已接收请求，等待响应")

            # 使用 litellm 调用（支持完整性校验重试）
            current_prompt = prompt
            retry_count = 0
            max_retries = config.report_integrity_retry if config.report_integrity_enabled else 0

            while True:
                start_time = time.time()
                try:
                    response_text, model_used, llm_usage = self._call_litellm(
                        current_prompt,
                        generation_config,
                        system_prompt=system_prompt,
                        stream=True,
                        stream_progress_callback=stream_progress_callback,
                        response_validator=self._validate_json_response,
                    )
                except _AllModelsFailedError as exc:
                    if exc.last_response_text is not None:
                        logger.warning(
                            "[LLM JSON] %s(%s): all models returned invalid JSON, using text fallback",
                            name,
                            code,
                        )
                        response_text = exc.last_response_text
                        model_used = exc.last_model
                        llm_usage = exc.last_usage
                    else:
                        raise
                elapsed = time.time() - start_time

                # 记录响应信息
                logger.info(
                    f"[LLM返回] {model_name} 响应成功, 耗时 {elapsed:.2f}s, 响应长度 {len(response_text)} 字符"
                )
                response_preview = response_text[:300] + "..." if len(response_text) > 300 else response_text
                logger.info(f"[LLM返回 预览]\n{response_preview}")
                logger.debug(
                    f"=== {model_name} 完整响应 ({len(response_text)}字符) ===\n{response_text}\n=== End Response ==="
                )
                # Keep parser/retry progress monotonic so task progress/message never "goes backward".
                parse_progress = min(99, 93 + retry_count * 2)
                _emit_progress(parse_progress, f"{name}：LLM 返回完成，正在解析 JSON")

                # 解析响应
                result = self._parse_response(response_text, code, name)
                result.raw_response = response_text
                result.search_performed = bool(news_context)
                result.market_snapshot = self._build_market_snapshot(context)
                result.model_used = model_used
                result.report_language = report_language
                result.analysis_mode = analysis_mode
                result.analysis_timeframe = analysis_timeframe
                normalize_chip_structure_availability(result, context.get("chip"))
                if is_crypto_context:
                    from src.services.crypto_backtest_service import fetch_btc_mfe_calibration

                    alignment_kwargs: dict[str, Any] = {
                        "runtime_config": config,
                        "trigger_context": context.get("trigger_context"),
                        "technical_context": context.get("crypto_technical"),
                    }
                    mfe_calibration = fetch_btc_mfe_calibration()
                    if mfe_calibration:
                        alignment_kwargs["mfe_calibration"] = mfe_calibration
                    align_btc_execution_plans(result, **alignment_kwargs)

                # 内容完整性校验（可选）
                if not config.report_integrity_enabled:
                    break
                require_phase_decision = isinstance(context.get("market_phase_context"), dict)
                pass_integrity, missing_fields = self._check_content_integrity(
                    result,
                    require_phase_decision=require_phase_decision,
                )
                if pass_integrity:
                    break
                if retry_count < max_retries:
                    current_prompt = self._build_integrity_retry_prompt(
                        prompt,
                        response_text,
                        missing_fields,
                        report_language=report_language,
                    )
                    retry_count += 1
                    logger.info(
                        "[LLM完整性] 必填字段缺失 %s，第 %d 次补全重试",
                        missing_fields,
                        retry_count,
                    )
                    retry_progress = min(99, 92 + retry_count * 2)
                    _emit_progress(
                        retry_progress,
                        f"{name}：报告字段不完整，正在补全重试（{retry_count}/{max_retries}）",
                    )
                else:
                    self._apply_placeholder_fill(result, missing_fields)
                    logger.warning(
                        "[LLM完整性] 必填字段缺失 %s，已占位补全，不阻塞流程",
                        missing_fields,
                    )
                    break

            persist_llm_usage(llm_usage, model_used, call_type="analysis", stock_code=code)

            logger.info(f"[LLM解析] {name}({code}) 分析完成: {result.trend_prediction}, 评分 {result.sentiment_score}")

            return result
            
        except Exception as e:
            logger.error(f"AI 分析 {name}({code}) 失败: {e}")
            return AnalysisResult(
                code=code,
                name=name,
                sentiment_score=50,
                trend_prediction='Sideways' if report_language == "en" else '震荡',
                operation_advice='Hold' if report_language == "en" else '持有',
                confidence_level='Low' if report_language == "en" else '低',
                analysis_summary=(f'Analysis failed: {str(e)[:100]}' if report_language == "en" else f'分析过程出错: {str(e)[:100]}'),
                risk_warning='Analysis failed. Please retry later or review manually.' if report_language == "en" else '分析失败，请稍后重试或手动分析',
                success=False,
                error_message=str(e),
                model_used=None,
                report_language=report_language,
                analysis_mode=analysis_mode,
                analysis_timeframe=analysis_timeframe,
            )
    
    def _format_prompt(
        self, 
        context: Dict[str, Any], 
        name: str,
        news_context: Optional[str] = None,
        report_language: str = "zh",
        analysis_context_pack_summary: Optional[str] = None,
    ) -> str:
        """
        格式化分析提示词（决策仪表盘 v2.0）
        
        包含：技术指标、实时行情（量比/换手率）、筹码分布、趋势分析、新闻
        
        Args:
            context: 技术面数据上下文（包含增强数据）
            name: 股票名称（默认值，可能被上下文覆盖）
            news_context: 预先搜索的新闻内容
        """
        code = context.get('code', 'Unknown')
        report_language = normalize_report_language(report_language)
        _, _, use_legacy_default_prompt = self._get_skill_prompt_sections()
        is_crypto_context = _is_crypto_analysis_context(context)
        if is_crypto_context:
            use_legacy_default_prompt = False
        
        # 优先使用上下文中的股票名称（从 realtime_quote 获取）
        stock_name = context.get('stock_name', name)
        if not stock_name or stock_name == f'股票{code}':
            stock_name = STOCK_NAME_MAP.get(code, f'股票{code}')
            
        today = context.get('today', {})
        unknown_text = get_unknown_text(report_language)
        no_data_text = get_no_data_text(report_language)
        quote_section_title, close_price_label = _phase_aware_quote_labels(context)
        subject_type = "加密货币" if is_crypto_context else "股票"
        subject_code_label = "交易标的" if is_crypto_context else "股票代码"
        subject_name_label = "标的名称" if is_crypto_context else "股票名称"
        price_unit = "USDT" if is_crypto_context else "元"
        amount_unit = "USDT" if is_crypto_context else "元"
        quantity_unit = "枚" if is_crypto_context else "股"
        if is_crypto_context:
            quote_section_title = "最新行情"
        hide_regular_session_ohlc = _should_hide_regular_session_ohlc(context)
        realtime_overlay_quote = hide_regular_session_ohlc and _today_has_realtime_overlay(today)
        pct_chg_label = "实时涨跌幅" if realtime_overlay_quote else "涨跌幅"
        volume_label = "实时成交量" if realtime_overlay_quote else "成交量"
        amount_label = "实时成交额" if realtime_overlay_quote else "成交额"
        quote_rows = [
            f"| {close_price_label} | {today.get('close', 'N/A')} {price_unit} |",
        ]
        if not hide_regular_session_ohlc:
            quote_rows.extend(
                [
                    f"| 开盘价 | {today.get('open', 'N/A')} {price_unit} |",
                    f"| 最高价 | {today.get('high', 'N/A')} {price_unit} |",
                    f"| 最低价 | {today.get('low', 'N/A')} {price_unit} |",
                ]
            )
        quote_rows.extend(
            [
                f"| {pct_chg_label} | {today.get('pct_chg', 'N/A')}% |",
                f"| {volume_label} | {self._format_volume(today.get('volume'), unit=quantity_unit)} |",
                f"| {amount_label} | {self._format_amount(today.get('amount'), unit=amount_unit)} |",
            ]
        )
        quote_rows_text = "\n".join(quote_rows)
        
        # ========== 构建决策仪表盘格式的输入 ==========
        prompt = f"""# 决策仪表盘分析请求

## 📊 {subject_type}基础信息
| 项目 | 数据 |
|------|------|
| {subject_code_label} | **{code}** |
| {subject_name_label} | **{stock_name}** |
| 分析日期 | {context.get('date', unknown_text)} |

---
"""
        prompt += format_market_phase_prompt_section(
            context.get("market_phase_context"),
            report_language=report_language,
        )
        daily_market_context_section = format_daily_market_context_prompt_section(
            context.get("daily_market_context"),
            report_language=report_language,
        )
        if daily_market_context_section:
            prompt += daily_market_context_section
        if isinstance(analysis_context_pack_summary, str) and analysis_context_pack_summary:
            prompt += analysis_context_pack_summary
        prompt += f"""

## 📈 技术面数据

### {quote_section_title}
| 指标 | 数值 |
|------|------|
{quote_rows_text}

### 均线系统（关键判断指标）
| 均线 | 数值 | 说明 |
|------|------|------|
| MA5 | {today.get('ma5', 'N/A')} | 短期趋势线 |
| MA10 | {today.get('ma10', 'N/A')} | 中短期趋势线 |
| MA20 | {today.get('ma20', 'N/A')} | 中期趋势线 |
| 均线形态 | {context.get('ma_status', unknown_text)} | 多头/空头/缠绕 |
"""
        
        # 添加实时行情数据（量比、换手率等）；BTC/crypto 使用专用交易框架，跳过股票专属字段。
        if 'realtime' in context and not is_crypto_context:
            rt = context['realtime']
            prompt += f"""
### 实时行情增强数据
| 指标 | 数值 | 解读 |
|------|------|------|
| 当前价格 | {rt.get('price', 'N/A')} 元 | |
| **量比** | **{rt.get('volume_ratio', 'N/A')}** | {rt.get('volume_ratio_desc', '')} |
| **换手率** | **{rt.get('turnover_rate', 'N/A')}%** | |
| 市盈率(动态) | {rt.get('pe_ratio', 'N/A')} | |
| 市净率 | {rt.get('pb_ratio', 'N/A')} | |
| 总市值 | {self._format_amount(rt.get('total_mv'))} | |
| 流通市值 | {self._format_amount(rt.get('circ_mv'))} | |
| 60日涨跌幅 | {rt.get('change_60d', 'N/A')}% | 中期表现 |
"""

        # 添加财报与分红（价值投资口径）
        fundamental_context = context.get("fundamental_context") if isinstance(context, dict) else None
        earnings_block = (
            fundamental_context.get("earnings", {})
            if isinstance(fundamental_context, dict)
            else {}
        )
        earnings_data = (
            earnings_block.get("data", {})
            if isinstance(earnings_block, dict)
            else {}
        )
        financial_report = (
            earnings_data.get("financial_report", {})
            if isinstance(earnings_data, dict)
            else {}
        )
        dividend_metrics = (
            earnings_data.get("dividend", {})
            if isinstance(earnings_data, dict)
            else {}
        )
        if not is_crypto_context and (isinstance(financial_report, dict) or isinstance(dividend_metrics, dict)):
            financial_report = financial_report if isinstance(financial_report, dict) else {}
            dividend_metrics = dividend_metrics if isinstance(dividend_metrics, dict) else {}
            ttm_yield = dividend_metrics.get("ttm_dividend_yield_pct", "N/A")
            ttm_cash = dividend_metrics.get("ttm_cash_dividend_per_share", "N/A")
            ttm_count = dividend_metrics.get("ttm_event_count", "N/A")
            report_date = financial_report.get("report_date", "N/A")
            prompt += f"""
### 财报与分红（价值投资口径）
| 指标 | 数值 | 说明 |
|------|------|------|
| 最近报告期 | {report_date} | 来自结构化财报字段 |
| 营业收入 | {financial_report.get('revenue', 'N/A')} | |
| 归母净利润 | {financial_report.get('net_profit_parent', 'N/A')} | |
| 经营现金流 | {financial_report.get('operating_cash_flow', 'N/A')} | |
| ROE | {financial_report.get('roe', 'N/A')} | |
| 近12个月每股现金分红 | {ttm_cash} | 仅现金分红、税前口径 |
| TTM 股息率 | {ttm_yield} | 公式：近12个月每股现金分红 / 当前价格 × 100% |
| TTM 分红事件数 | {ttm_count} | |

> 若上述字段为 N/A 或缺失，请明确写“数据缺失，无法判断”，禁止编造。
"""

        capital_flow_block = (
            fundamental_context.get("capital_flow", {})
            if isinstance(fundamental_context, dict)
            else {}
        )
        capital_flow_data = (
            capital_flow_block.get("data", {})
            if isinstance(capital_flow_block, dict)
            else {}
        )
        stock_flow = (
            capital_flow_data.get("stock_flow", {})
            if isinstance(capital_flow_data, dict)
            else {}
        )
        sector_flow = (
            capital_flow_data.get("sector_rankings", {})
            if isinstance(capital_flow_data, dict)
            else {}
        )
        has_capital_flow = (
            isinstance(stock_flow, dict)
            and any(v is not None for v in stock_flow.values())
        ) or (
            isinstance(sector_flow, dict)
            and (sector_flow.get("top") or sector_flow.get("bottom"))
        )
        if has_capital_flow and not is_crypto_context:
            top_sectors = sector_flow.get("top", []) if isinstance(sector_flow, dict) else []
            bottom_sectors = sector_flow.get("bottom", []) if isinstance(sector_flow, dict) else []
            top_sector_text = "、".join(
                str(item.get("name", "")).strip()
                for item in top_sectors[:3]
                if isinstance(item, dict) and str(item.get("name", "")).strip()
            ) or "N/A"
            bottom_sector_text = "、".join(
                str(item.get("name", "")).strip()
                for item in bottom_sectors[:3]
                if isinstance(item, dict) and str(item.get("name", "")).strip()
            ) or "N/A"
            prompt += f"""
### 主力资金流向（操作建议过滤器）
| 指标 | 数值 | 决策含义 |
|------|------|----------|
| 主力净流入 | {stock_flow.get('main_net_inflow', 'N/A')} | 正值偏支持，负值偏压制 |
| 5日净流入 | {stock_flow.get('inflow_5d', 'N/A')} | 用于判断资金持续性 |
| 10日净流入 | {stock_flow.get('inflow_10d', 'N/A')} | 用于判断资金持续性 |
| 资金流入靠前板块 | {top_sector_text} | 板块资金共振参考 |
| 资金流出靠前板块 | {bottom_sector_text} | 板块风险参考 |

> 资金流向只能作为价格位置的过滤器：接近压力且主力流出时不得追买；接近支撑且未放量跌破时，优先判断为持有观察、震荡或洗盘观察。
"""

        # 添加筹码分布数据；BTC/crypto 不使用 A 股筹码口径。
        if 'chip' in context and not is_crypto_context:
            chip = context['chip']
            profit_ratio = chip.get('profit_ratio', 0)
            prompt += f"""
### 筹码分布数据（效率指标）
| 指标 | 数值 | 健康标准 |
|------|------|----------|
| **获利比例** | **{profit_ratio:.1%}** | 70-90%时警惕 |
| 平均成本 | {chip.get('avg_cost', 'N/A')} 元 | 现价应高于5-15% |
| 90%筹码集中度 | {chip.get('concentration_90', 0):.2%} | <15%为集中 |
| 70%筹码集中度 | {chip.get('concentration_70', 0):.2%} | |
| 筹码状态 | {chip.get('chip_status', unknown_text)} | |
"""
        elif not is_crypto_context:
            chip_unavailable_text = get_chip_unavailable_text(report_language)
            chip_instruction = (
                "Do not fabricate profit ratio, average cost, or concentration. Mention chip data "
                "unavailability only once in the report; do not repeat per-field no-data text in `chip_structure`."
                if report_language == "en"
                else "请勿编造获利比例、平均成本或集中度；报告中只说明一次筹码数据不可用，不要把“数据缺失，无法判断”逐字段重复写入 `chip_structure`。"
            )
            prompt += f"""
### 筹码分布数据（效率指标）
> {chip_unavailable_text}
> {chip_instruction}
"""
        
        # 添加趋势分析结果（仅隐式内建 bull_trend 默认回退保留旧口径）
        if 'trend_analysis' in context:
            trend = _sanitize_trend_analysis_for_prompt(
                context['trend_analysis'],
                volume_change_ratio=context.get('volume_change_ratio'),
            )
            consistency_notes = trend.get('prompt_consistency_notes', [])
            if use_legacy_default_prompt:
                bias_warning = "🚨 超过5%，严禁追高！" if trend.get('bias_ma5', 0) > 5 else "✅ 安全范围"
                prompt += f"""
### 趋势分析预判（基于交易理念）
| 指标 | 数值 | 判定 |
|------|------|------|
| 趋势状态 | {trend.get('trend_status', unknown_text)} | |
| 均线排列 | {trend.get('ma_alignment', unknown_text)} | MA5>MA10>MA20为多头 |
| 趋势强度 | {trend.get('trend_strength', 0)}/100 | |
| **乖离率(MA5)** | **{trend.get('bias_ma5', 0):+.2f}%** | {bias_warning} |
| 乖离率(MA10) | {trend.get('bias_ma10', 0):+.2f}% | |
| 量能状态 | {trend.get('volume_status', unknown_text)} | {trend.get('volume_trend', '')} |
| 系统信号 | {trend.get('buy_signal', unknown_text)} | |
| 系统评分 | {trend.get('signal_score', 0)}/100 | |

#### 系统分析理由
**买入理由**：
{chr(10).join('- ' + r for r in trend.get('signal_reasons', ['无'])) if trend.get('signal_reasons') else '- 无'}

**风险因素**：
{chr(10).join('- ' + r for r in trend.get('risk_factors', ['无'])) if trend.get('risk_factors') else '- 无'}
"""
                if consistency_notes:
                    prompt += f"""

**一致性约束**：
{chr(10).join('- ' + note for note in consistency_notes)}
"""
            else:
                bias_warning = (
                    "🚨 偏离较大，需谨慎评估追高风险"
                    if trend.get('bias_ma5', 0) > 5
                    else "✅ 位置相对可控"
                )
                prompt += f"""
### 技术与结构分析（供激活技能判断参考）
| 指标 | 数值 | 说明 |
|------|------|------|
| 趋势状态 | {trend.get('trend_status', unknown_text)} | |
| 均线排列 | {trend.get('ma_alignment', unknown_text)} | 结合激活技能判断结构强弱 |
| 趋势强度 | {trend.get('trend_strength', 0)}/100 | |
| **价格位置(MA5)** | **{trend.get('bias_ma5', 0):+.2f}%** | {bias_warning} |
| 价格位置(MA10) | {trend.get('bias_ma10', 0):+.2f}% | |
| 量能状态 | {trend.get('volume_status', unknown_text)} | {trend.get('volume_trend', '')} |
| 系统信号 | {trend.get('buy_signal', unknown_text)} | |
| 系统评分 | {trend.get('signal_score', 0)}/100 | |

#### 系统分析理由
**支持因素**：
{chr(10).join('- ' + r for r in trend.get('signal_reasons', ['无'])) if trend.get('signal_reasons') else '- 无'}

**风险因素**：
{chr(10).join('- ' + r for r in trend.get('risk_factors', ['无'])) if trend.get('risk_factors') else '- 无'}
"""
                if consistency_notes:
                    prompt += f"""

**一致性约束**：
{chr(10).join('- ' + note for note in consistency_notes)}
"""

        crypto_technical = context.get("crypto_technical") if isinstance(context, dict) else None
        trigger_context = context.get("trigger_context") if isinstance(context, dict) else None
        analysis_mode = str(context.get("analysis_mode") or "daily").strip().lower() if isinstance(context, dict) else "daily"
        if isinstance(crypto_technical, dict):
            runtime_config = self._get_runtime_config()
            minimum_risk_reward = float(
                getattr(runtime_config, "crypto_backtest_minimum_risk_reward", 1.2)
            )
            minimum_volume_ratio = float(
                getattr(runtime_config, "crypto_backtest_minimum_volume_ratio", 1.0)
            )
            taker_fee_bps = max(float(getattr(runtime_config, "crypto_backtest_taker_fee_rate_bps", 5.0)), 0.0)
            slippage_bps = max(float(getattr(runtime_config, "crypto_backtest_slippage_bps", 2.0)), 0.0)
            neutral_band_pct = max(float(getattr(runtime_config, "crypto_backtest_neutral_band_pct", 0.2)), 0.0)
            round_trip_cost_bps = 2.0 * taker_fee_bps + 2.0 * slippage_bps
            round_trip_cost_pct = round_trip_cost_bps / 100.0
            minimum_target_buffer_pct = round_trip_cost_pct + neutral_band_pct
            timeframes = crypto_technical.get("timeframes") or {}
            daily_crypto = timeframes.get("daily") if isinstance(timeframes.get("daily"), dict) else crypto_technical
            hourly_crypto = timeframes.get("hourly") if isinstance(timeframes.get("hourly"), dict) else None
            intraday = crypto_technical.get("intraday") if isinstance(crypto_technical.get("intraday"), dict) else {}
            price_action = daily_crypto.get("price_action") or {}
            fib = daily_crypto.get("fibonacci") or {}
            fib_levels = fib.get("retracement_levels") or {}
            volume = daily_crypto.get("volume") or {}
            volatility = daily_crypto.get("volatility") or {}
            daily_volatility_forecast = (
                volatility.get("forecast") if isinstance(volatility.get("forecast"), dict) else {}
            )
            vwap = daily_crypto.get("vwap") or {}
            ema = daily_crypto.get("ema") or {}
            mode_instruction = (
                "本轮是 BTC 小时线分析：日线只作为背景和风险边界，重点刷新 `intraday_plan`。"
                "小时线允许出现与日线方向相反的短线机会，但必须明确写成“逆日线短线/日内机会”，"
                "给出更严格止损、较短有效期和失效条件；不要把小时线机会升级为日线主方向反转。"
                if analysis_mode == "hourly"
                else "本轮是 BTC 日线主分析：需要完整评估日线级 `long_plan`、`short_plan`，并结合小时线给出独立的日内执行机会。"
            )
            prompt += f"""
### BTC 交易框架补充（日线，必须纳入结论）
| 维度 | 当前读数 | 分析要求 |
|------|----------|----------|
| Price Action（价格行为） | 状态={price_action.get('state', unknown_text)}；前20根高点/阻力={price_action.get('recent_high', 'N/A')}；前20根低点/支撑={price_action.get('recent_low', 'N/A')}；最新涨跌={price_action.get('close_change_pct', 'N/A')}%；扫过前高={price_action.get('high_swept', 'N/A')}；收盘站上阻力={price_action.get('close_above_resistance', 'N/A')}；扫过前低={price_action.get('low_swept', 'N/A')}；收盘跌破支撑={price_action.get('close_below_support', 'N/A')} | 必须区分实体收盘突破/跌破与插针扫高扫低；只有收盘站上阻力才是真突破，插针扫过前高但收不住应视为流动性掠夺/假突破风险 |
| Fibonacci（斐波那契回调） | swing high={fib.get('swing_high', 'N/A')}；swing low={fib.get('swing_low', 'N/A')}；38.2%={fib_levels.get('38.2%', 'N/A')}；50%={fib_levels.get('50.0%', 'N/A')}；61.8%={fib_levels.get('61.8%', 'N/A')} | 将这些位置作为潜在支撑/阻力，给出回踩和失守观察点 |
| Volume（成交量） | 最新={volume.get('latest', 'N/A')}；均量={volume.get('average', 'N/A')}；量比={volume.get('ratio', 'N/A')}；确认={volume.get('confirmation', 'N/A')} | 判断突破/跌破是否有成交量确认，低量突破必须降权 |
| Volatility / ATR（波动率） | ATR14={volatility.get('atr14', 'N/A')}；ATR14%={volatility.get('atr14_pct', 'N/A')}% | 止损与目标价必须参考 ATR，避免把止损放在日常波动噪音内；若 ATR 缺失，明确说明波动率数据不足 |
| EWMA 波动预测 | 数据质量={daily_volatility_forecast.get('data_quality', 'N/A')}；下一根 sigma={daily_volatility_forecast.get('forecast_sigma_pct', 'N/A')}%；历史分位={daily_volatility_forecast.get('historical_percentile', 'N/A')}%；状态={daily_volatility_forecast.get('regime', 'N/A')}；仓位上限乘数={daily_volatility_forecast.get('position_multiplier_cap', 'N/A')} | 只用于仓位和风险预算，不预测方向；elevated/extreme 必须缩减仓位，compressed 不得自动加杠杆，需等待突破确认 |
| VWAP（成交量加权均价） | rolling20 VWAP={vwap.get('rolling_20', 'N/A')}；价格位置={vwap.get('price_position', 'N/A')} | 价格在 VWAP 上方偏多头强势，下方偏空头压制；日线数据下按 rolling VWAP 解读 |
| EMA（指数移动平均） | EMA20={ema.get('ema20', 'N/A')}；EMA50={ema.get('ema50', 'N/A')}；结构={ema.get('structure', 'N/A')} | 判断短中期趋势延续或反转，必须和 MA/MACD/RSI 交叉验证 |

> BTC 日线特别要求：日线是主方向和风险边界，最终操作建议必须同时引用 Price Action、Fibonacci、Volume、VWAP、EMA、ATR 中至少三个维度；若这些维度互相冲突，优先输出“等待确认/区间策略”，不要给激进追涨、抄底或盲目开空建议。
> BTC 突破判定规则：真突破必须满足实体收盘站上前高/阻力，不能仅因最高价扫过前高就判定突破；若 `high_swept=true` 但 `close_above_resistance=false`，必须按“流动性掠夺/假突破偏空风险”处理，并降低多单置信度。跌破同理：真跌破必须收盘跌破支撑；若只插针扫低后收回，优先按流动性掠夺/假跌破反弹风险处理。
> BTC 冲突判定规则：当短线 Price Action 与中线 EMA/VWAP 冲突时，定义为“反弹/回调或短线推进”，不得直接升级为趋势反转；只有 Price Action、VWAP、EMA、Volume 至少三项同向确认，才可称为趋势延续或反转。
> BTC 本轮分析模式：{mode_instruction}
> BTC 双向交易要求：BTC 支持多单和空单，分析时必须同时评估 Long/多单与 Short/空单，不得只给多单买入视角。若多头条件更强，给出多单入场、止损、目标和失效条件；若空头条件更强，给出空单入场/做空开仓、止损、目标和失效条件；若多空都不满足，明确“不做多也不做空，等待确认”。
> BTC 日线策略点位强制结构：`dashboard.battle_plan.long_plan` 和 `dashboard.battle_plan.short_plan` 只承载日线级主策略，必须同时输出，分别包含 `plan_type`、`direction`、`entry_zone`、`entry_price`、`stop_loss`、`take_profit`、`execution_ladder`、`execution_contract`、`trigger_condition`、`invalidation`、`invalid_condition`、`risk_reward`、`position_hint`、`confidence`、`no_trade_reason`、`reason`。即使最终倾向一边，也要给出另一边的“仅在何条件触发”的备用计划；若某方向暂不满足，写清等待触发条件和 `no_trade_reason`，不要省略该方向。
> BTC 小时线日内计划强制结构：如果存在小时线数据，必须输出 `dashboard.battle_plan.intraday_plan`，并明确 `plan_type`、`enabled`、`direction`、`entry_zone`、`entry_price`、`stop_loss`、`take_profit`、`execution_ladder`、`execution_contract`、`trigger_condition`、`invalidation`、`invalid_condition`、`daily_constraint`、`risk_reward`、`position_hint`、`confidence`、`no_trade_reason`、`reason`。这个字段只承载 1 小时线日内交易建议，不得把日线主策略写进这里；如果没有日内机会，`enabled=false`、`direction="wait"`，并写清等待条件和 `no_trade_reason`。
> BTC 分步执行要求：每个计划必须输出 `execution_ladder`，将交易动作拆为 `trial_entry`（试仓）、`confirmation_add`（确认加仓）和 `invalidation`（撤销/退出）。可交易计划的顶层 `entry_price`、`entry_zone`、`trigger_condition` 与 `execution_contract` 只表示试仓层，必须分别与 `trial_entry.entry_price`、`trial_entry.entry_zone`、`trial_entry.trigger_condition` 一致；`stop_loss` 必须与 `invalidation.price` 一致。确认加仓只能在试仓方向已验证后执行，不能用来替换试仓价或掩盖追价风险。`current_action` 只能是 `trial` 或 `wait`：价格已在试仓区且结构成立时写 `trial`，否则写 `wait` 并说明等待条件。`scenario` 只能是 `trend_pullback`、`liquidity_sweep_reversal`、`breakout_continuation`、`range_rejection` 或 `wait`。未触发时各层可设 `enabled=false`，但仍必须给出明确的等待价或失效价；不得把“观望”作为没有点位和条件的泛化结论。
> BTC 可回测执行契约：所有 `direction=long/short` 且可交易的 BTC 计划必须同时输出 `execution_contract`，版本固定为 `btc-execution-v1`。`instrument` 必须完整保留系统给出的 `type`、`venue`、`symbol`、`market_symbol`、成交/触发/强平价格类型和保证金模式，不得把现货与永续互换。`entry.setup_type` 只能是 `breakout` 或 `pullback`。入场条件只允许 `close_above`、`close_below`、`low_lte`、`high_gte`、`volume_ratio_gte`、`volume_ratio_lte`、`close_above_vwap`、`close_below_vwap`；`logic` 固定为 `all`，`fill` 固定为 `next_bar_open`，并给出 `confirmation_bars`、`max_wait_bars` 和 `exit.max_holding_bars`。契约必须完整表达 `trigger_condition`，禁止把“站稳、放量、企稳”等额外条件只写在自然语言里。无法用这些原语完整表达时必须设为不交易，不得输出会被降级为触价成交的计划。
> `execution_contract.entry.conditions[].type` 必须从上述单个枚举值中选择，禁止输出带 `/` 的组合占位字符串。`breakout` 多单通常使用 `close_above` + `volume_ratio_gte` + `close_above_vwap`，空单使用对应的向下条件；`pullback` 多单应使用 `low_lte` 触及支撑后配合 `close_above`/`close_above_vwap` 收回确认，空单使用 `high_gte` 触及压力后配合 `close_below`/`close_below_vwap`。
> BTC 点位字段格式：`entry_price`、`stop_loss`、`take_profit` 只能填写单个正数，不得混入“突破、回踩、站稳、跌破”等说明文字；区间放入 `entry_zone`，确认逻辑放入 `trigger_condition`，失效说明放入 `invalid_condition`。
> BTC 计划质量门槛：可交易计划必须满足多单 `stop_loss < entry_price < take_profit`、空单 `take_profit < entry_price < stop_loss`；计划风险收益比不得低于 1:{minimum_risk_reward:g}。当前按 taker 双边手续费 {taker_fee_bps:g}bps/边、双边滑点 {2 * slippage_bps:g}bps、中性收益带 {neutral_band_pct:g}% 计算，往返成本约 {round_trip_cost_bps:g}bps（{round_trip_cost_pct:.4f}%），目标毛空间至少先覆盖 {minimum_target_buffer_pct:.4f}%；实际目标还必须满足 `reward_distance >= {minimum_risk_reward:g} * risk_distance`，并在此基础上再覆盖上述成本缓冲，否则设为不交易。
> BTC 计划选择规则：趋势已经明确但突破追价会让风险收益不足时，优先生成 `pullback` 计划并冻结支撑/压力、止损、目标和有效 bars，不得在后续报告中随着价格上涨持续抬高同一方向的触发价。只有原计划过期、失效或方向反转后才能启用新计划。
> BTC 量能与确认门槛：`breakout` 计划必须包含 `volume_ratio_gte` 且阈值不得低于 {minimum_volume_ratio:g}，普通突破/跌破使用至少 2 根闭合 K 线确认；`pullback` 计划以触及关键位后收回为硬条件，量能只用于置信度和仓位降权，不强制作为硬门槛。Price Action、VWAP、EMA 未形成可解释结构，或日线/小时线方向处于 `wait_for_*` 等冲突状态时，默认 `enabled=false`/等待确认，不得勉强给出入场计划。
> BTC `sniper_points` 兼容规则：`dashboard.battle_plan.sniper_points` 只填写最终主方案的点位，并在文字中标明方向；完整的两套点位必须放入 `long_plan` 与 `short_plan`。
> BTC `decision_type` 兼容规则：JSON 字段仍只能使用 `buy`、`hold`、`sell`；为避免合约语义歧义，`buy` 仅表示 Long / 多单开仓或加多，`sell` 仅表示 Short / 空单开仓、加空或多单风控退出，`hold` 表示 Flat / 空仓等待、持仓观望或区间观察。若建议做空，`operation_advice`、`dashboard.core_conclusion.position_advice` 与 `dashboard.battle_plan.sniper_points` 的文字必须明确写“空单入场/做空开仓”，不要写成单纯“卖出现货”；若是平空或平多，必须在文字中明确写“平空/平多”，不要只依赖 `buy`/`sell`。
"""
            hourly_price_action = hourly_crypto.get("price_action") if isinstance(hourly_crypto, dict) else {}
            hourly_volume = hourly_crypto.get("volume") if isinstance(hourly_crypto, dict) else {}
            hourly_volatility = hourly_crypto.get("volatility") if isinstance(hourly_crypto, dict) else {}
            hourly_volatility_forecast = (
                hourly_volatility.get("forecast")
                if isinstance(hourly_volatility.get("forecast"), dict)
                else {}
            )
            hourly_vwap = hourly_crypto.get("vwap") if isinstance(hourly_crypto, dict) else {}
            hourly_ema = hourly_crypto.get("ema") if isinstance(hourly_crypto, dict) else {}
            hourly_event = hourly_crypto.get("event") if isinstance(hourly_crypto, dict) else {}
            hourly_trigger_reference = (
                hourly_event.get("trigger_reference") if isinstance(hourly_event.get("trigger_reference"), dict) else {}
            )
            derivatives = crypto_technical.get("derivatives") if isinstance(crypto_technical.get("derivatives"), dict) else {}
            funding = derivatives.get("funding") if isinstance(derivatives.get("funding"), dict) else {}
            open_interest = derivatives.get("open_interest") if isinstance(derivatives.get("open_interest"), dict) else {}
            funding_history = funding.get("history_7d") if isinstance(funding.get("history_7d"), dict) else {}
            oi_history = open_interest.get("history_24h") if isinstance(open_interest.get("history_24h"), dict) else {}
            basis = derivatives.get("basis") if isinstance(derivatives.get("basis"), dict) else {}
            long_short_ratio = derivatives.get("long_short_ratio") if isinstance(derivatives.get("long_short_ratio"), dict) else {}
            cross_exchange = derivatives.get("cross_exchange") if isinstance(derivatives.get("cross_exchange"), dict) else {}
            order_flow = derivatives.get("order_flow") if isinstance(derivatives.get("order_flow"), dict) else {}
            macro_correlation = crypto_technical.get("macro_correlation") if isinstance(crypto_technical.get("macro_correlation"), dict) else {}
            macro_assets = macro_correlation.get("assets") if isinstance(macro_correlation.get("assets"), dict) else {}
            nasdaq_corr = macro_assets.get("nasdaq") if isinstance(macro_assets.get("nasdaq"), dict) else {}
            dxy_corr = macro_assets.get("dxy") if isinstance(macro_assets.get("dxy"), dict) else {}
            us10y_corr = macro_assets.get("us10y") if isinstance(macro_assets.get("us10y"), dict) else {}
            gold_corr = macro_assets.get("gold") if isinstance(macro_assets.get("gold"), dict) else {}
            hourly_opportunity = intraday.get("opportunity", "N/A")
            if not hourly_crypto:
                hourly_opportunity = "小时线数据缺失，日内交易计划必须设为 enabled=false、direction=\"wait\"，等待小时线数据恢复或新的日内触发。"
            if isinstance(trigger_context, dict) and trigger_context.get("trigger_reason") in {"volatility_spike", "entry_signal"}:
                prompt += f"""
### BTC 波动触发上下文（日内触发，必须解释）
| 字段 | 数值 |
|------|------|
| 触发来源 | {trigger_context.get('trigger_source', 'btc_volatility')} |
| 触发原因 | {trigger_context.get('trigger_reason', 'entry_signal')} |
| 短窗口方向 | {trigger_context.get('direction', 'N/A')} |
| 短窗口涨跌幅 | {trigger_context.get('change_pct', 'N/A')}% |
| 当前价格 | {trigger_context.get('price', 'N/A')} |
| 基准价格 | {trigger_context.get('baseline_price', 'N/A')} |
| 机会价格 | {trigger_context.get('opportunity_price', 'N/A')} |
| 建议交易方向 | {trigger_context.get('trade_direction', 'N/A')} |
| 入场信号动作 | {trigger_context.get('suggested_trade_action', 'N/A')} |
| 入场确认价 | {trigger_context.get('entry_price', 'N/A')} |
| 入场确认幅度 | {trigger_context.get('entry_confirmation_pct', 'N/A')}% |
| 失效价 | {trigger_context.get('invalidation_price', 'N/A')} |
| 失效幅度 | {trigger_context.get('invalidation_pct', 'N/A')}% |
| 观察秒数 | {trigger_context.get('watched_seconds', 'N/A')} |
| 窗口秒数 | {trigger_context.get('window_seconds', 'N/A')} |
| 触发阈值 | {trigger_context.get('threshold_pct', 'N/A')}% |
| 确认采样 | {trigger_context.get('confirmation_count', 'N/A')}/{trigger_context.get('confirmation_required', 'N/A')} |
| 脉冲阶段 | {trigger_context.get('impulse_stage', 'N/A')} |
| 当前可执行 | {trigger_context.get('entry_executable_now', 'N/A')} |
| 确认价越过幅度 | {trigger_context.get('entry_overshoot_pct', 'N/A')}% |
| 追价上限/下限 | {trigger_context.get('no_chase_price', 'N/A')} |
| 脉冲极值 / 回撤 | {trigger_context.get('impulse_extreme_price', 'N/A')} / {trigger_context.get('impulse_retrace_pct', 'N/A')}% |
| 行情时间 | {trigger_context.get('provider_timestamp', 'N/A')} |

> 本轮小时线分析由日内价格机会监控触发，不是普通整点小时线复盘。若 `触发原因=entry_signal` 且 `当前可执行=1`、`脉冲阶段=early_continuation`，必须把“入场确认价、失效价、观察秒数、建议交易方向”写入 `dashboard.battle_plan.intraday_plan.trigger_condition`、`invalidation` 或 `reason`，形成可执行的多/空入场信号。若 `当前可执行=0`，或脉冲阶段为 `late_extension`/`exhaustion_candidate`，`intraday_plan` 必须 `enabled=false`、`direction="wait"`，禁止把虚构的理想回踩价包装成当前建议；只可说明未来重新满足的确认条件和失效条件。当前 1 小时 K 线可能尚未收线，短窗口冲击只能作为日内触发/风控上下文，不能直接升级为日线趋势反转结论。
"""
            prompt += f"""
### BTC 小时线日内交易机会（独立判断）
| 维度 | 当前读数 | 分析要求 |
|------|----------|----------|
| 日线偏向 | {intraday.get('daily_bias', 'N/A')} | 作为背景、仓位和风险边界；不得直接否决小时线相反方向的短线机会 |
| 小时线偏向 | {intraday.get('hourly_bias', 'N/A')} | 只用于入场、加减仓和日内触发判断 |
| 对齐状态 | {intraday.get('alignment', 'N/A')} | aligned_long/short 表示顺日线机会；conflict/countertrend 表示逆日线短线机会，需要更严格风控 |
| 小时线 Price Action | 状态={hourly_price_action.get('state', unknown_text)}；前20根高点/阻力={hourly_price_action.get('recent_high', 'N/A')}；前20根低点/支撑={hourly_price_action.get('recent_low', 'N/A')}；最新涨跌={hourly_price_action.get('close_change_pct', 'N/A')}%；扫过前高={hourly_price_action.get('high_swept', 'N/A')}；收盘站上阻力={hourly_price_action.get('close_above_resistance', 'N/A')} | 日内触发必须区分真突破与插针扫高；扫高回落不得作为多单突破触发 |
| 小时线 Volume/VWAP/EMA/ATR | 量比={hourly_volume.get('ratio', 'N/A')}；量能确认={hourly_volume.get('confirmation', 'N/A')}；VWAP={hourly_vwap.get('rolling_20', 'N/A')}；价格位置={hourly_vwap.get('price_position', 'N/A')}；EMA20={hourly_ema.get('ema20', 'N/A')}；EMA50={hourly_ema.get('ema50', 'N/A')}；结构={hourly_ema.get('structure', 'N/A')}；ATR14={hourly_volatility.get('atr14', 'N/A')}；ATR14%={hourly_volatility.get('atr14_pct', 'N/A')}%；EWMA sigma={hourly_volatility_forecast.get('forecast_sigma_pct', 'N/A')}%；波动状态={hourly_volatility_forecast.get('regime', 'N/A')}；仓位上限乘数={hourly_volatility_forecast.get('position_multiplier_cap', 'N/A')} | 判断日内触发是否有量价、均线和波动率确认；止损不得落在小时线常规 ATR 噪音内，elevated/extreme 必须缩仓 |
| 小时线急跌/扫低事件 | 类型={hourly_event.get('type', 'N/A')}；建议方向={hourly_event.get('suggested_direction', 'N/A')}；紧急度={hourly_event.get('urgency', 'N/A')}；参考高点={hourly_event.get('reference_high', 'N/A')}；事件低点={hourly_event.get('event_low', 'N/A')}；事件K高点={hourly_event.get('event_bar_high', 'N/A')}；高点到低点跌幅={hourly_event.get('drop_from_reference_high_pct', 'N/A')}%；低点反弹={hourly_event.get('rebound_from_event_low_pct', 'N/A')}%；ATR位移={hourly_event.get('atr_move', 'N/A')}；多单确认价={hourly_trigger_reference.get('long_confirmation_price', 'N/A')}；多单失效价={hourly_trigger_reference.get('long_invalidation_price', 'N/A')}；空单跌破价={hourly_trigger_reference.get('short_breakdown_price', 'N/A')} | 若出现 `sharp_selloff_*`、`selloff_rebound_*` 或 `liquidity_sweep_low_reversal_candidate`，不得只写泛泛观望；必须给出“上破多单确认价才进场”和“跌破空单跌破价则放弃抄底/转空”的明确二选一条件 |
| 日内机会 | {hourly_opportunity} | 必须写清是否有日内交易机会；没有机会时说明等待什么小时线条件 |
| 衍生品杠杆环境 | 数据质量={derivatives.get('data_quality', 'N/A')}；资金费率={funding.get('rate_pct', 'N/A')}%；状态={funding.get('state', 'N/A')}；7日均值={funding_history.get('avg_rate_pct', 'N/A')}%；趋势={funding_history.get('trend', 'N/A')}；持仓量={open_interest.get('value', 'N/A')} BTC；名义规模={open_interest.get('notional_usdt', 'N/A')} USDT；OI 24h变化={oi_history.get('change_pct', 'N/A')}%；OI趋势={oi_history.get('state', 'N/A')}；永续基差={basis.get('perpetual_premium_pct', 'N/A')}%；基差状态={basis.get('state', 'N/A')}；多空比={long_short_ratio.get('current', 'N/A')}；多空比状态={long_short_ratio.get('state', 'N/A')}；跨所质量={cross_exchange.get('data_quality', 'N/A')}；跨所Funding差={cross_exchange.get('funding_spread_pct', 'N/A')}%；杠杆压力={derivatives.get('leverage_pressure', 'N/A')} | Funding 为正且偏高时警惕多头拥挤和追多回撤；Funding 为负且偏深时警惕空头拥挤和 short squeeze；结合 Funding 趋势、OI 变化、基差和多空比判断杠杆扩张/去杠杆，跨所只有单源时必须降置信度；数据缺失必须标记为不确定而不是中性 |
| 主动买卖量 / CVD（影子因子） | 数据质量={order_flow.get('data_quality', 'N/A')}；窗口={order_flow.get('window_minutes', 'N/A')}分钟；主动买入占比={order_flow.get('taker_buy_ratio_pct', 'N/A')}%；CVD={order_flow.get('cvd_base_volume', 'N/A')} BTC；CVD占成交量={order_flow.get('cvd_pct_of_volume', 'N/A')}%；价格变化={order_flow.get('price_change_pct', 'N/A')}%；状态={order_flow.get('state', 'N/A')}；背离={order_flow.get('divergence', 'N/A')} | 仅作为突破/回踩执行质量的影子确认因子，不得单独决定多空；价格上涨但 CVD 偏空时降低追多置信度，价格下跌但 CVD 偏多时警惕假跌破/空头衰竭 |
| 跨市场相关性 | 数据质量={macro_correlation.get('data_quality', 'N/A')}；纳指30/60日={nasdaq_corr.get('correlation_30d', 'N/A')}/{nasdaq_corr.get('correlation_60d', 'N/A')}，状态={nasdaq_corr.get('state', 'N/A')}；DXY30/60日={dxy_corr.get('correlation_30d', 'N/A')}/{dxy_corr.get('correlation_60d', 'N/A')}，状态={dxy_corr.get('state', 'N/A')}；美债10Y30/60日={us10y_corr.get('correlation_30d', 'N/A')}/{us10y_corr.get('correlation_60d', 'N/A')}，状态={us10y_corr.get('state', 'N/A')}；黄金30/60日={gold_corr.get('correlation_30d', 'N/A')}/{gold_corr.get('correlation_60d', 'N/A')}，状态={gold_corr.get('state', 'N/A')} | 相关性只用于识别风险因子暴露和仓位降权，不作为单独入场依据；纳指高正相关时视为科技风险资产暴露，DXY/美债相关性突变时降低方向置信度；样本不足不得推断 |

> BTC 小时线约束：小时线是独立的日内机会层，不再强制服从日线方向。日线偏空但小时线出现多单机会、或日线偏多但小时线出现空单机会时，可以给出逆日线短线计划，但必须明确这是日内/短线机会，止损更严格、仓位更轻、有效期更短，并写清触发价、止损、目标、失效条件和 `daily_constraint`。必须写入 `dashboard.battle_plan.intraday_plan`；若小时线数据缺失或没有日内机会，`enabled=false`、`direction="wait"`，并说明等待条件。
> BTC 急跌机会约束：当“小时线急跌/扫低事件”的类型不是 `none` 时，`dashboard.battle_plan.intraday_plan.trigger_condition` 必须引用“多单确认价”或“空单跌破价”中的具体数值；`invalidation` 必须引用“多单失效价”或事件低点；`reason` 必须说明这是急跌后的右侧确认/假跌破反弹/跌破延续，禁止只输出“暂无明确信号”而不给可执行等待价位。
> BTC 衍生品约束：Funding/OI 只作为杠杆拥挤度和风控降权信息，不得单独作为入场依据；当 `leverage_pressure` 显示 long/short crowding risk 时，必须在对应方向计划中降低仓位、等待更强价格确认，或解释为什么当前结构足以抵消拥挤风险。
> BTC 订单流约束：CVD/主动买卖量当前处于影子验证阶段，只能用于降低置信度、要求更强价格确认或解释假突破风险；不得因单个订单流窗口直接反转日线/小时线方向，也不得把缺失订单流解释为中性。
"""
        
        # 添加昨日对比数据
        if 'yesterday' in context:
            volume_change = context.get('volume_change_ratio', 'N/A')
            prompt += f"""
### 量价变化
- 成交量较昨日变化：{volume_change}倍
- 价格较昨日变化：{context.get('price_change_ratio', 'N/A')}%
"""
            parsed_volume_change = _safe_float(volume_change, default=math.nan)
            if math.isfinite(parsed_volume_change) and parsed_volume_change > 10:
                prompt += """
- ⚠️ 量能异常提示：成交量较昨日放大超过10倍，可能受异常数据或一次性冲量影响，必须降权解读，不能机械视为强确认信号
"""
        
        # 添加新闻搜索结果（重点区域）
        news_window_days: Optional[int] = None
        context_window = context.get("news_window_days")
        try:
            if context_window is not None:
                parsed_window = int(context_window)
                if parsed_window > 0:
                    news_window_days = parsed_window
        except (TypeError, ValueError):
            news_window_days = None

        if news_window_days is None:
            prompt_config = self._get_runtime_config()
            news_window_days = resolve_news_window_days(
                news_max_age_days=getattr(prompt_config, "news_max_age_days", 3),
                news_strategy_profile=getattr(prompt_config, "news_strategy_profile", "short"),
            )
        prompt += """
---

## 📰 舆情情报
"""
        if news_context:
            if is_crypto_context:
                prompt += f"""
以下是 **{stock_name}({code})** 近{news_window_days}日的加密市场信息，请重点提取：
1. 🚨 **风险警报**：监管、交易所风险、链上异常、流动性收缩、宏观风险
2. 🎯 **利好催化**：ETF/机构资金流、链上活跃度改善、流动性改善、宏观风险偏好回升
3. 📊 **市场驱动**：美元指数、利率预期、美股风险偏好、稳定币流动性、衍生品杠杆与资金费率
4. 🕒 **时间规则（强制）**：
   - 输出到 `risk_alerts` / `positive_catalysts` / `latest_news` 的每一条都必须带具体日期（YYYY-MM-DD）
   - 超出近{news_window_days}日窗口的新闻一律忽略
   - 时间未知、无法确定发布日期的新闻一律忽略

```
{news_context}
```
"""
            else:
                prompt += f"""
以下是 **{stock_name}({code})** 近{news_window_days}日的新闻搜索结果，请重点提取：
1. 🚨 **风险警报**：减持、处罚、利空
2. 🎯 **利好催化**：业绩、合同、政策
3. 📊 **业绩预期**：年报预告、业绩快报
4. 🕒 **时间规则（强制）**：
   - 输出到 `risk_alerts` / `positive_catalysts` / `latest_news` 的每一条都必须带具体日期（YYYY-MM-DD）
   - 超出近{news_window_days}日窗口的新闻一律忽略
   - 时间未知、无法确定发布日期的新闻一律忽略

```
{news_context}
```
"""
        else:
            no_news_subject = "该加密货币近期的市场新闻" if is_crypto_context else "该股票近期的相关新闻"
            prompt += f"""
未搜索到{no_news_subject}。请主要依据技术面数据进行分析。
"""

        # 注入缺失数据警告
        if context.get('data_missing'):
            prompt += """
⚠️ **数据缺失警告**
由于接口限制，当前无法获取完整的实时行情和技术指标数据。
请 **忽略上述表格中的 N/A 数据**，重点依据 **【📰 舆情情报】** 中的新闻进行基本面和情绪面分析。
在回答技术面问题（如均线、乖离率）时，请直接说明“数据缺失，无法判断”，**严禁编造数据**。
"""

        # 明确的输出要求
        prompt += f"""
---

## ✅ 分析任务

请为 **{stock_name}({code})** 生成【决策仪表盘】，严格按照 JSON 格式输出。
"""
        if context.get('is_index_etf'):
            prompt += """
> ⚠️ **指数/ETF 分析约束**：该标的为指数跟踪型 ETF 或市场指数。
> - 风险分析仅关注：**指数走势、跟踪误差、市场流动性**
> - 严禁将基金公司的诉讼、声誉、高管变动纳入风险警报
> - 业绩预期基于**指数成分股整体表现**，而非基金公司财报
> - `risk_alerts` 中不得出现基金管理人相关的公司经营风险

"""
        if is_crypto_context:
            prompt += f"""
### ⚠️ 重要：输出正确的标的名称格式
正确的标的名称格式为“标的名称（交易标的）”，例如“Bitcoin（BTC）”。
如果上方显示的标的名称不正确，请在分析开头**明确输出该标的的正确名称**。
"""
        else:
            prompt += f"""
### ⚠️ 重要：输出正确的股票名称格式
正确的股票名称格式为“股票名称（股票代码）”，例如“贵州茅台（600519）”。
如果上方显示的股票名称为"股票{code}"或不正确，请在分析开头**明确输出该股票的正确中文全称**。
"""
        if use_legacy_default_prompt:
            prompt += f"""

### 重点关注（必须明确回答）：
1. ❓ 是否满足 MA5>MA10>MA20 多头排列？
2. ❓ 当前乖离率是否在安全范围内（<5%）？—— 超过5%必须标注"严禁追高"
3. ❓ 量能是否配合（缩量回调/放量突破）？
4. ❓ 筹码结构是否健康？
5. ❓ 消息面有无重大利空？（减持、处罚、业绩变脸等）
"""
        elif is_crypto_context:
            prompt += f"""

### 重点关注（必须明确回答）：
1. ❓ 日线 Price Action、Fibonacci、Volume、VWAP、EMA 是否形成多空共振？
2. ❓ 当前价格相对关键支撑/阻力、VWAP、EMA 的风险回报是否合理？若不合理，请明确等待条件
3. ❓ 成交量、波动结构与小时线执行层是否支持当前结论？
4. ❓ 消息面/宏观/链上或流动性信息是否与技术结论冲突？
5. ❓ 多单与空单各自的触发价、止损价、目标价、确认条件和失效条件是什么？
"""
        else:
            prompt += f"""

### 重点关注（必须明确回答）：
1. ❓ 当前结构是否满足激活技能的关键触发条件？
2. ❓ 当前入场位置与风险回报是否合理？若偏离过大，请明确说明等待条件
3. ❓ 量能、波动与筹码结构是否支持当前结论？
4. ❓ 消息面有无重大利空或与技能结论冲突的信息？
5. ❓ 若结论成立，具体触发条件、止损位、观察点分别是什么？
"""
        if isinstance(crypto_technical, dict) and not is_crypto_context:
            prompt += """
6. ❓ BTC 专项：Price Action、Fibonacci、Volume、VWAP、EMA 对多单与空单分别是否形成共振？多单/空单各自的触发价、止损价、目标价、确认条件和失效条件是什么？
"""
        if is_crypto_context:
            prompt += f"""

### 决策仪表盘要求：
- **标的名称**：必须输出正确名称（如"Bitcoin"或"比特币"，不要输出"股票BTC"）
- **核心结论**：一句话说清当前应做多、做空、减仓风控还是等待
- **仓位分类建议**：空仓者怎么做 vs 已持有多单/空单者怎么做
- **具体交易计划**：必须同时输出日线级 `long_plan` 与 `short_plan` 两套策略，且每套都包含入场价、止损价、目标价、触发条件、失效条件和依据；若存在小时线数据，必须额外输出 `intraday_plan`，单独描述 1 小时线执行机会，不能与日线主策略混写
- **检查清单**：每项用 ✅/⚠️/❌ 标记
- **消息面时间合规**：`latest_news`、`risk_alerts`、`positive_catalysts` 不得包含超出近{news_window_days}日或时间未知的信息
- **技术面一致性**：严禁把互斥结论同时当作有效依据；若消息面/宏观/链上信息与技术面冲突，必须明确写“事件先行、技术待确认”或“消息面偏多/偏空，但技术面尚未确认”
 
请输出完整的 JSON 格式决策仪表盘。"""
        else:
            prompt += f"""

### 决策仪表盘要求：
- **股票名称**：必须输出正确的中文全称（如"贵州茅台"而非"股票600519"）
- **核心结论**：一句话说清该买/该卖/该等
- **持仓分类建议**：空仓者怎么做 vs 持仓者怎么做
- **具体狙击点位**：买入价、止损价、目标价（精确到分）；BTC 必须同时输出日线级 `long_plan` 与 `short_plan` 两套策略，且每套都包含入场价、止损价、目标价、触发条件、失效条件和依据；若存在小时线数据，必须额外输出 `intraday_plan`，单独描述 1 小时线日内机会，不能与日线主策略混写
- **检查清单**：每项用 ✅/⚠️/❌ 标记
- **消息面时间合规**：`latest_news`、`risk_alerts`、`positive_catalysts` 不得包含超出近{news_window_days}日或时间未知的信息
- **技术面一致性**：严禁把“空头排列”和“多头排列”等互斥结论同时当作有效依据；若基本面/事件面与技术面冲突，必须明确写“事件先行、技术待确认”或“基本面偏多，但技术面尚未确认”
 
请输出完整的 JSON 格式决策仪表盘。"""

        if report_language == "en":
            prompt += """

### Output language requirements (highest priority)
- Keep every JSON key exactly as defined above; do not translate keys.
- `decision_type` must remain `buy`, `hold`, or `sell`.
- All human-readable JSON values must be in English.
- This includes `stock_name`, `trend_prediction`, `operation_advice`, `confidence_level`, all nested dashboard text, checklist items, and every summary field.
- Use the common English company name when you are confident. If not, keep the listed company name rather than inventing one.
- When data is missing, explain it in English instead of Chinese.
"""
        else:
            prompt += f"""

### 输出语言要求（最高优先级）
- 所有 JSON 键名必须保持不变，不要翻译键名。
- `decision_type` 必须保持为 `buy`、`hold`、`sell`。
- 所有面向用户的人类可读文本值必须使用中文。
- 当数据缺失时，请使用中文直接说明“{no_data_text}，无法判断”。
"""
        
        return prompt
    
    def _format_volume(self, volume: Optional[float], *, unit: str = "股") -> str:
        """格式化成交量显示"""
        if volume is None:
            return 'N/A'
        if volume >= 1e8:
            return f"{volume / 1e8:.2f} 亿{unit}"
        elif volume >= 1e4:
            return f"{volume / 1e4:.2f} 万{unit}"
        else:
            return f"{volume:.0f} {unit}"
    
    def _format_amount(self, amount: Optional[float], *, unit: str = "元") -> str:
        """格式化成交额显示"""
        if amount is None:
            return 'N/A'
        if unit != "元":
            if amount >= 1e8:
                return f"{amount / 1e8:.2f} 亿 {unit}"
            elif amount >= 1e4:
                return f"{amount / 1e4:.2f} 万 {unit}"
            return f"{amount:.0f} {unit}"
        if amount >= 1e8:
            return f"{amount / 1e8:.2f} 亿元"
        elif amount >= 1e4:
            return f"{amount / 1e4:.2f} 万元"
        else:
            return f"{amount:.0f} 元"

    def _format_percent(self, value: Optional[float]) -> str:
        """格式化百分比显示"""
        if value is None:
            return 'N/A'
        try:
            return f"{float(value):.2f}%"
        except (TypeError, ValueError):
            return 'N/A'

    def _format_price(self, value: Optional[float]) -> str:
        """格式化价格显示"""
        if value is None:
            return 'N/A'
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return 'N/A'

    def _build_market_snapshot(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """构建当日行情快照（展示用）"""
        today = context.get('today', {}) or {}
        realtime = context.get('realtime', {}) or {}
        yesterday = context.get('yesterday', {}) or {}

        prev_close = yesterday.get('close')
        close = today.get('close')
        high = today.get('high')
        low = today.get('low')

        amplitude = None
        change_amount = None
        if prev_close not in (None, 0) and high is not None and low is not None:
            try:
                amplitude = (float(high) - float(low)) / float(prev_close) * 100
            except (TypeError, ValueError, ZeroDivisionError):
                amplitude = None
        if prev_close is not None and close is not None:
            try:
                change_amount = float(close) - float(prev_close)
            except (TypeError, ValueError):
                change_amount = None

        snapshot = {
            "date": context.get('date', '未知'),
            "close": self._format_price(close),
            "open": self._format_price(today.get('open')),
            "high": self._format_price(high),
            "low": self._format_price(low),
            "prev_close": self._format_price(prev_close),
            "pct_chg": self._format_percent(today.get('pct_chg')),
            "change_amount": self._format_price(change_amount),
            "amplitude": self._format_percent(amplitude),
            "volume": self._format_volume(today.get('volume')),
            "amount": self._format_amount(today.get('amount')),
        }

        if realtime:
            snapshot.update({
                "price": self._format_price(realtime.get('price')),
                "volume_ratio": realtime.get('volume_ratio', 'N/A'),
                "turnover_rate": self._format_percent(realtime.get('turnover_rate')),
                "source": getattr(realtime.get('source'), 'value', realtime.get('source', 'N/A')),
            })

        return snapshot

    def _check_content_integrity(
        self,
        result: AnalysisResult,
        *,
        require_phase_decision: bool = False,
    ) -> Tuple[bool, List[str]]:
        """Delegate to module-level check_content_integrity."""
        return check_content_integrity(result, require_phase_decision=require_phase_decision)

    def _build_integrity_complement_prompt(self, missing_fields: List[str], report_language: str = "zh") -> str:
        """Build complement instruction for missing mandatory fields."""
        report_language = normalize_report_language(report_language)
        if report_language == "en":
            lines = ["### Completion requirements: fill the missing mandatory fields below and output the full JSON again:"]
            for f in missing_fields:
                if f == "sentiment_score":
                    lines.append("- sentiment_score: integer score from 0 to 100")
                elif f == "operation_advice":
                    lines.append("- operation_advice: localized action advice")
                elif f == "analysis_summary":
                    lines.append("- analysis_summary: concise analysis summary")
                elif f == "dashboard.core_conclusion.one_sentence":
                    lines.append("- dashboard.core_conclusion.one_sentence: one-line decision")
                elif f == "dashboard.intelligence.risk_alerts":
                    lines.append("- dashboard.intelligence.risk_alerts: risk alert list (can be empty)")
                elif f == "dashboard.battle_plan.sniper_points.stop_loss":
                    lines.append("- dashboard.battle_plan.sniper_points.stop_loss: stop-loss level")
                elif f == "dashboard.phase_decision.phase_context":
                    lines.append("- dashboard.phase_decision.phase_context: public market phase summary subset")
                elif f == "dashboard.phase_decision.action_window":
                    lines.append("- dashboard.phase_decision.action_window: phase-aware action window")
                elif f == "dashboard.phase_decision.immediate_action":
                    lines.append("- dashboard.phase_decision.immediate_action: act now / wait / watch / no intraday action")
                elif f == "dashboard.phase_decision.watch_conditions":
                    lines.append("- dashboard.phase_decision.watch_conditions: list of watch conditions")
                elif f == "dashboard.phase_decision.next_check_time":
                    lines.append("- dashboard.phase_decision.next_check_time: next check point or market-local time")
                elif f == "dashboard.phase_decision.confidence_reason":
                    lines.append("- dashboard.phase_decision.confidence_reason: confidence rationale and data limits")
                elif f == "dashboard.phase_decision.data_limitations":
                    lines.append("- dashboard.phase_decision.data_limitations: list of phase/data quality limitations")
            return "\n".join(lines)

        lines = ["### 补全要求：请在上方分析基础上补充以下必填内容，并输出完整 JSON："]
        for f in missing_fields:
            if f == "sentiment_score":
                lines.append("- sentiment_score: 0-100 综合评分")
            elif f == "operation_advice":
                lines.append("- operation_advice: 买入/加仓/持有/减仓/卖出/观望")
            elif f == "analysis_summary":
                lines.append("- analysis_summary: 综合分析摘要")
            elif f == "dashboard.core_conclusion.one_sentence":
                lines.append("- dashboard.core_conclusion.one_sentence: 一句话决策")
            elif f == "dashboard.intelligence.risk_alerts":
                lines.append("- dashboard.intelligence.risk_alerts: 风险警报列表（可为空数组）")
            elif f == "dashboard.battle_plan.sniper_points.stop_loss":
                lines.append("- dashboard.battle_plan.sniper_points.stop_loss: 止损价")
            elif f == "dashboard.phase_decision.phase_context":
                lines.append("- dashboard.phase_decision.phase_context: 公开低敏市场阶段摘要子集")
            elif f == "dashboard.phase_decision.action_window":
                lines.append("- dashboard.phase_decision.action_window: 阶段化行动窗口")
            elif f == "dashboard.phase_decision.immediate_action":
                lines.append("- dashboard.phase_decision.immediate_action: 立即行动/等待确认/观察/无盘中动作")
            elif f == "dashboard.phase_decision.watch_conditions":
                lines.append("- dashboard.phase_decision.watch_conditions: 观察条件数组")
            elif f == "dashboard.phase_decision.next_check_time":
                lines.append("- dashboard.phase_decision.next_check_time: 下一次检查点或市场本地时间")
            elif f == "dashboard.phase_decision.confidence_reason":
                lines.append("- dashboard.phase_decision.confidence_reason: 置信度理由与数据限制")
            elif f == "dashboard.phase_decision.data_limitations":
                lines.append("- dashboard.phase_decision.data_limitations: 阶段/数据质量限制数组")
        return "\n".join(lines)

    def _build_integrity_retry_prompt(
        self,
        base_prompt: str,
        previous_response: str,
        missing_fields: List[str],
        report_language: str = "zh",
    ) -> str:
        """Build retry prompt using the previous response as the complement baseline."""
        complement = self._build_integrity_complement_prompt(missing_fields, report_language=report_language)
        previous_output = previous_response.strip()
        if normalize_report_language(report_language) == "en":
            prefix = "### The previous output is below. Complete the missing fields based on that output and return the full JSON again. Do not omit existing fields:"
        else:
            prefix = "### 上一次输出如下，请在该输出基础上补齐缺失字段，并重新输出完整 JSON。不要省略已有字段："
        return "\n\n".join([
            base_prompt,
            prefix,
            previous_output,
            complement,
        ])

    def _apply_placeholder_fill(self, result: AnalysisResult, missing_fields: List[str]) -> None:
        """Delegate to module-level apply_placeholder_fill."""
        apply_placeholder_fill(result, missing_fields)

    def _parse_response(
        self, 
        response_text: str, 
        code: str, 
        name: str
    ) -> AnalysisResult:
        """
        解析 Gemini 响应（决策仪表盘版）
        
        尝试从响应中提取 JSON 格式的分析结果，包含 dashboard 字段
        如果解析失败，尝试智能提取或返回默认结果
        """
        try:
            report_language = normalize_report_language(
                getattr(self._get_runtime_config(), "report_language", "zh")
            )
            analysis_mode = "daily"
            analysis_timeframe = analysis_timeframe_label(analysis_mode, report_language)
            # 清理响应文本：移除 markdown 代码块标记
            cleaned_text = response_text
            if '```json' in cleaned_text:
                cleaned_text = cleaned_text.replace('```json', '').replace('```', '')
            elif '```' in cleaned_text:
                cleaned_text = cleaned_text.replace('```', '')
            
            # 尝试找到 JSON 内容
            json_start = cleaned_text.find('{')
            json_end = cleaned_text.rfind('}') + 1
            
            if json_start >= 0 and json_end > json_start:
                json_str = cleaned_text[json_start:json_end]
                
                # 尝试修复常见的 JSON 问题
                json_str = self._fix_json_string(json_str)
                
                data = json.loads(json_str)

                # Schema validation (lenient: on failure, continue with raw dict)
                try:
                    AnalysisReportSchema.model_validate(data)
                except Exception as e:
                    logger.warning(
                        "LLM report schema validation failed, continuing with raw dict: %s",
                        str(e)[:100],
                    )

                # 提取 dashboard 数据
                dashboard = data.get('dashboard', None)

                # 优先使用 AI 返回的股票名称（如果原名称无效或包含代码）
                ai_stock_name = data.get('stock_name')
                if ai_stock_name and (name.startswith('股票') or name == code or 'Unknown' in name):
                    name = ai_stock_name

                # 解析所有字段，使用默认值防止缺失
                # 解析 decision_type，如果没有则根据 operation_advice 推断
                decision_type = data.get('decision_type', '')
                if not decision_type:
                    op = data.get('operation_advice', 'Hold' if report_language == "en" else '持有')
                    decision_type = infer_decision_type_from_advice(op, default='hold')
                
                explicit_action = data.get("action")
                if explicit_action is None and isinstance(dashboard, dict):
                    explicit_action = dashboard.get("action")

                result = AnalysisResult(
                    code=code,
                    name=name,
                    # 核心指标
                    sentiment_score=int(data.get('sentiment_score', 50)),
                    trend_prediction=data.get('trend_prediction', 'Sideways' if report_language == "en" else '震荡'),
                    operation_advice=data.get('operation_advice', 'Hold' if report_language == "en" else '持有'),
                    decision_type=decision_type,
                    confidence_level=localize_confidence_level(
                        data.get('confidence_level', 'Medium' if report_language == "en" else '中'),
                        report_language,
                    ),
                    report_language=report_language,
                    analysis_mode=analysis_mode,
                    analysis_timeframe=analysis_timeframe,
                    # 决策仪表盘
                    dashboard=dashboard,
                    # 走势分析
                    trend_analysis=data.get('trend_analysis', ''),
                    short_term_outlook=data.get('short_term_outlook', ''),
                    medium_term_outlook=data.get('medium_term_outlook', ''),
                    # 技术面
                    technical_analysis=data.get('technical_analysis', ''),
                    ma_analysis=data.get('ma_analysis', ''),
                    volume_analysis=data.get('volume_analysis', ''),
                    pattern_analysis=data.get('pattern_analysis', ''),
                    # 基本面
                    fundamental_analysis=data.get('fundamental_analysis', ''),
                    sector_position=data.get('sector_position', ''),
                    company_highlights=data.get('company_highlights', ''),
                    # 情绪面/消息面
                    news_summary=data.get('news_summary', ''),
                    market_sentiment=data.get('market_sentiment', ''),
                    hot_topics=data.get('hot_topics', ''),
                    # 综合
                    analysis_summary=data.get('analysis_summary', 'Analysis completed' if report_language == "en" else '分析完成'),
                    key_points=data.get('key_points', ''),
                    risk_warning=data.get('risk_warning', ''),
                    buy_reason=data.get('buy_reason', ''),
                    # 元数据
                    search_performed=data.get('search_performed', False),
                    data_sources=data.get('data_sources', 'Technical data' if report_language == "en" else '技术面数据'),
                    success=True,
                )
                return populate_decision_action_fields(result, explicit_action=explicit_action)
            else:
                # 没有找到 JSON，标记为失败
                logger.warning(f"无法从响应中提取 JSON，标记为解析失败")
                return self._parse_text_response(response_text, code, name)
                
        except json.JSONDecodeError as e:
            logger.warning(f"JSON 解析失败: {e}，标记为解析失败")
            return self._parse_text_response(response_text, code, name)
    
    def _fix_json_string(self, json_str: str) -> str:
        """修复常见的 JSON 格式问题"""
        import re
        
        # 移除注释
        json_str = re.sub(r'//.*?\n', '\n', json_str)
        json_str = re.sub(r'/\*.*?\*/', '', json_str, flags=re.DOTALL)
        
        # 修复尾随逗号
        json_str = re.sub(r',\s*}', '}', json_str)
        json_str = re.sub(r',\s*]', ']', json_str)
        
        # 确保布尔值是小写
        json_str = json_str.replace('True', 'true').replace('False', 'false')
        
        # fix by json-repair
        json_str = repair_json(json_str)
        
        return json_str

    def _validate_json_response(self, text: str) -> None:
        """Validate that *text* contains a parseable JSON object.

        Used as the ``response_validator`` argument to :meth:`_call_litellm` so
        that a JSON-less or unparseable reply from the primary model is treated
        as a model failure and triggers fallback to the next configured model.

        Raises:
            ValueError: if no JSON object is found in *text*.
            json.JSONDecodeError: if the extracted JSON cannot be parsed (after
                :meth:`_fix_json_string` attempts repair).
        """
        cleaned = text
        if "```json" in cleaned:
            cleaned = cleaned.replace("```json", "").replace("```", "")
        elif "```" in cleaned:
            cleaned = cleaned.replace("```", "")

        json_start = cleaned.find("{")
        json_end = cleaned.rfind("}") + 1

        if json_start < 0 or json_end <= json_start:
            raise ValueError("No JSON object found in LLM response")

        json_str = cleaned[json_start:json_end]
        json_str = self._fix_json_string(json_str)
        json.loads(json_str)
    
    def _parse_text_response(
        self, 
        response_text: str, 
        code: str, 
        name: str
    ) -> AnalysisResult:
        """从纯文本响应中尽可能提取分析信息"""
        report_language = normalize_report_language(
            getattr(self._get_runtime_config(), "report_language", "zh")
        )
        analysis_mode = "daily"
        analysis_timeframe = analysis_timeframe_label(analysis_mode, report_language)
        # 尝试识别关键词来判断情绪
        sentiment_score = 50
        trend = 'Sideways' if report_language == "en" else '震荡'
        advice = 'Hold' if report_language == "en" else '持有'
        
        text_lower = response_text.lower()
        
        # 简单的情绪识别
        positive_keywords = ['看多', '买入', '上涨', '突破', '强势', '利好', '加仓', 'bullish', 'buy']
        negative_keywords = ['看空', '卖出', '下跌', '跌破', '弱势', '利空', '减仓', 'bearish', 'sell']
        
        positive_count = sum(1 for kw in positive_keywords if kw in text_lower)
        negative_count = sum(1 for kw in negative_keywords if kw in text_lower)
        
        if positive_count > negative_count + 1:
            sentiment_score = 65
            trend = 'Bullish' if report_language == "en" else '看多'
            advice = 'Buy' if report_language == "en" else '买入'
            decision_type = 'buy'
        elif negative_count > positive_count + 1:
            sentiment_score = 35
            trend = 'Bearish' if report_language == "en" else '看空'
            advice = 'Sell' if report_language == "en" else '卖出'
            decision_type = 'sell'
        else:
            decision_type = 'hold'
        
        # 截取前500字符作为摘要
        summary = response_text[:500] if response_text else ('No analysis result' if report_language == "en" else '无分析结果')
        
        result = AnalysisResult(
            code=code,
            name=name,
            sentiment_score=sentiment_score,
            trend_prediction=trend,
            operation_advice=advice,
            decision_type=decision_type,
            confidence_level='Low' if report_language == "en" else '低',
            analysis_summary=summary,
            key_points='JSON parsing failed; treat this as best-effort output.' if report_language == "en" else 'JSON解析失败，仅供参考',
            risk_warning='The result may be inaccurate. Cross-check with other information.' if report_language == "en" else '分析结果可能不准确，建议结合其他信息判断',
            raw_response=response_text,
            success=False,
            error_message='LLM response is not valid JSON; analysis result will not be persisted',
            report_language=report_language,
            analysis_mode=analysis_mode,
            analysis_timeframe=analysis_timeframe,
        )
        return populate_decision_action_fields(result)
    
    def batch_analyze(
        self, 
        contexts: List[Dict[str, Any]],
        delay_between: float = 2.0
    ) -> List[AnalysisResult]:
        """
        批量分析多只股票
        
        注意：为避免 API 速率限制，每次分析之间会有延迟
        
        Args:
            contexts: 上下文数据列表
            delay_between: 每次分析之间的延迟（秒）
            
        Returns:
            AnalysisResult 列表
        """
        results = []
        
        for i, context in enumerate(contexts):
            if i > 0:
                logger.debug(f"等待 {delay_between} 秒后继续...")
                time.sleep(delay_between)
            
            result = self.analyze(context)
            results.append(result)
        
        return results


# 便捷函数
def get_analyzer() -> GeminiAnalyzer:
    """获取 LLM 分析器实例"""
    return GeminiAnalyzer()


if __name__ == "__main__":
    # 测试代码
    logging.basicConfig(level=logging.DEBUG)
    
    # 模拟上下文数据
    test_context = {
        'code': '600519',
        'date': '2026-01-09',
        'today': {
            'open': 1800.0,
            'high': 1850.0,
            'low': 1780.0,
            'close': 1820.0,
            'volume': 10000000,
            'amount': 18200000000,
            'pct_chg': 1.5,
            'ma5': 1810.0,
            'ma10': 1800.0,
            'ma20': 1790.0,
            'volume_ratio': 1.2,
        },
        'ma_status': '多头排列 📈',
        'volume_change_ratio': 1.3,
        'price_change_ratio': 1.5,
    }
    
    analyzer = GeminiAnalyzer()
    
    if analyzer.is_available():
        print("=== AI 分析测试 ===")
        result = analyzer.analyze(test_context)
        print(f"分析结果: {result.to_dict()}")
    else:
        print("Gemini API 未配置，跳过测试")
