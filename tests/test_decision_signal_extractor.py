# -*- coding: utf-8 -*-
"""Tests for extracting DecisionSignal assets from completed reports."""

from __future__ import annotations

import os

import pytest

from src.analyzer import AnalysisResult
from src.config import Config
from src.services.decision_signal_extractor import (
    _apply_crypto_plan_freeze,
    build_decision_signal_payload_from_report,
    extract_and_persist_from_analysis_result,
)
import src.services.decision_signal_extractor as decision_signal_extractor_module
from src.services.decision_signal_service import DecisionSignalService
from src.storage import DatabaseManager


@pytest.fixture()
def isolated_db(tmp_path):
    old_database_path = os.environ.get("DATABASE_PATH")
    db_path = tmp_path / "decision_signal_extractor.db"
    os.environ["DATABASE_PATH"] = str(db_path)
    Config.reset_instance()
    DatabaseManager.reset_instance()
    db = DatabaseManager.get_instance()
    try:
        yield db
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        if old_database_path is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = old_database_path


def _result(**overrides) -> AnalysisResult:
    result = AnalysisResult(
        code="BTC",
        name="Bitcoin",
        sentiment_score=82,
        trend_prediction="看多",
        operation_advice="买入",
        decision_type="buy",
        confidence_level="高",
        analysis_summary="趋势确认，量价配合。",
        risk_warning="跌破支撑需止损",
        report_language="zh",
    )
    result.dashboard = {
        "battle_plan": {
            "sniper_points": {
                "ideal_buy": "理想买入点：1700元",
                "secondary_buy": "1680-1690（回踩MA5附近）",
                "stop_loss": "止损位：1600元",
                "take_profit": "目标位：1850元",
            },
            "action_checklist": ["放量突破前高", "回踩不破MA10"],
        },
        "phase_decision": {
            "watch_conditions": ["盘中量能继续放大"],
        },
        "intelligence": {
            "risk_alerts": ["估值偏高"],
            "positive_catalysts": ["业绩超预期"],
        },
    }
    for key, value in overrides.items():
        setattr(result, key, value)
    return result


def test_build_payload_maps_report_context_and_price_plan() -> None:
    result = _result()
    result.market_phase_summary = {"phase": "postmarket"}
    result.analysis_context_pack_overview = {"data_quality": {"overall_score": 55, "level": "fair"}}
    context_snapshot = {
        "market_phase_summary": {
            "phase": "intraday",
            "session_date": "2026-06-15",
            "minutes_to_open": None,
            "minutes_to_close": 120,
        },
        "analysis_context_pack_overview": {
            "data_quality": {"overall_score": 91, "level": "good"},
        },
    }

    payload = build_decision_signal_payload_from_report(
        result,
        context_snapshot=context_snapshot,
        source_report_id=88,
        trace_id="trace-88",
        query_source="api",
        report_type="full",
    )

    assert payload is not None
    assert payload["stock_code"] == "BTC"
    assert payload["stock_name"] == "Bitcoin"
    assert payload["market"] == "crypto"
    assert payload["source_type"] == "analysis"
    assert payload["source_report_id"] == 88
    assert payload["trace_id"] == "trace-88"
    assert payload["trigger_source"] == "api"
    assert payload["action"] == "buy"
    assert payload["confidence"] == 0.8
    assert payload["score"] == 82
    assert payload["market_phase"] == "intraday"
    assert payload["entry_low"] == 1690.0
    assert payload["entry_high"] == 1700.0
    assert payload["stop_loss"] == 1600.0
    assert payload["target_price"] == 1850.0
    assert payload["data_quality_summary"]["overall_score"] == 91
    assert payload["watch_conditions"] == ["盘中量能继续放大"]
    assert payload["risk_summary"] == ["跌破支撑需止损", "估值偏高"]
    assert payload["catalyst_summary"] == ["业绩超预期"]
    assert payload["metadata"]["report_confidence_level"] == "高"
    assert payload["metadata"]["market_phase_summary"] == {
        "phase": "intraday",
        "session_date": "2026-06-15",
        "minutes_to_close": 120,
    }


def test_build_payload_prefers_active_intraday_strategy_plan() -> None:
    result = _result(
        operation_advice="观望",
        decision_type="hold",
        confidence_level="中",
    )
    result.action = "watch"
    result.dashboard = {
        "battle_plan": {
            "sniper_points": {
                "ideal_buy": "1700",
                "stop_loss": "1600",
                "take_profit": "1850",
            },
            "intraday_plan": {
                "plan_type": "intraday",
                "strategy_class": "selloff_rebound_trial",
                "enabled": True,
                "direction": "long",
                "entry_zone": "1710-1720",
                "entry_price": "1715",
                "stop_loss": "1688",
                "take_profit": "1760",
                "trigger_condition": "小时线放量突破 1720 后回踩不破",
                "invalidation": "跌破 1688 或突破失败回落",
                "invalid_condition": "小时线收盘跌破 1688",
                "daily_constraint": "日线必须守住 1680",
                "risk_reward": "1:2.1",
                "position_hint": "单笔风险不超过 0.5%",
                "confidence": "中：等待右侧确认",
                "reason": "急跌后收复 VWAP，存在日内右侧机会",
                "trigger_execution_state": "selloff_rebound_trial_ready",
                "selloff_rebound_control": {
                    "confirmation_price": 1715,
                    "invalidation_price": 1688,
                },
                "execution_contract": {
                    "version": "btc-execution-v1",
                    "entry": {"setup_type": "pullback"},
                },
            },
        },
        "phase_decision": {
            "watch_conditions": ["确认突破后再执行"],
        },
    }

    payload = build_decision_signal_payload_from_report(
        result,
        context_snapshot={
            "analysis_mode": "hourly",
            "market_phase_summary": {"phase": "intraday", "minutes_to_close": 180},
        },
        source_report_id=188,
        trace_id="trace-intraday",
        query_source="btc_volatility",
        report_type="simple",
    )

    assert payload is not None
    assert payload["action"] == "buy"
    assert payload["horizon"] == "intraday"
    assert payload["entry_low"] == 1710.0
    assert payload["entry_high"] == 1720.0
    assert payload["stop_loss"] == 1688.0
    assert payload["target_price"] == 1760.0
    assert payload["invalidation"] == "小时线收盘跌破 1688"
    assert payload["watch_conditions"] == [
        "小时线放量突破 1720 后回踩不破",
        "日线必须守住 1680",
        "确认突破后再执行",
    ]
    assert payload["reason"] == "急跌后收复 VWAP，存在日内右侧机会"
    assert payload["metadata"]["strategy_plan"]["source"] == "intraday_plan"
    assert payload["metadata"]["strategy_plan"]["strategy_class"] == "selloff_rebound_trial"
    assert payload["metadata"]["strategy_plan"]["trigger_execution_state"] == (
        "selloff_rebound_trial_ready"
    )
    assert payload["metadata"]["strategy_plan"]["selloff_rebound_control"] == {
        "confirmation_price": 1715,
        "invalidation_price": 1688,
    }
    assert payload["metadata"]["strategy_plan"]["setup_type"] == "pullback"
    assert payload["metadata"]["strategy_plan"]["risk_reward"] == "1:2.1"
    assert payload["evidence"]["strategy_plan"]["setup_type"] == "pullback"
    assert payload["evidence"]["strategy_plan"]["position_hint"] == "单笔风险不超过 0.5%"


def test_build_payload_uses_result_fallbacks_and_optional_catalysts() -> None:
    result = _result(confidence_level="低")
    result.dashboard = {
        "battle_plan": {
            "sniper_points": {"ideal_buy": "1700"},
            "action_checklist": ["等待回踩确认"],
        },
        "intelligence": {},
    }
    result.market_phase_summary = {"phase": "postmarket"}
    result.analysis_context_pack_overview = {"data_quality": {"level": "limited"}}

    payload = build_decision_signal_payload_from_report(
        result,
        context_snapshot=None,
        source_report_id=None,
        trace_id="trace-fallback",
        query_source="",
        report_type="simple",
    )

    assert payload is not None
    assert payload["market_phase"] == "postmarket"
    assert payload["data_quality_summary"] == {"level": "limited"}
    assert payload["entry_low"] == 1700.0
    assert "entry_high" not in payload
    assert payload["watch_conditions"] == ["等待回踩确认"]
    assert "catalyst_summary" not in payload
    assert payload["trigger_source"] == "system"
    assert payload["confidence"] == 0.4


def test_build_payload_maps_secondary_only_entry_to_entry_high() -> None:
    result = _result()
    result.dashboard = {
        "battle_plan": {
            "sniper_points": {"secondary_buy": "次优买入点：1680元"},
        },
    }

    payload = build_decision_signal_payload_from_report(
        result,
        trace_id="trace-secondary-only",
        query_source="api",
        report_type="simple",
    )

    assert payload is not None
    assert "entry_low" not in payload
    assert payload["entry_high"] == 1680.0


def test_build_payload_reuses_shared_sniper_fallback_paths(isolated_db) -> None:
    result = _result()
    result.dashboard = {}
    result.raw_response = {
        "dashboard": {
            "battle_plan": {
                "sniper_points": {
                    "ideal_buy": "1690",
                    "secondary_buy": "1705",
                    "stop_loss": "1620",
                    "take_profit": "1880",
                }
            }
        }
    }

    payload = build_decision_signal_payload_from_report(
        result,
        trace_id="trace-raw-sniper",
        query_source="api",
        report_type="simple",
    )
    stored_points = isolated_db._extract_sniper_points(result)

    assert payload is not None
    assert stored_points == {
        "ideal_buy": 1690.0,
        "secondary_buy": 1705.0,
        "stop_loss": 1620.0,
        "take_profit": 1880.0,
    }
    assert payload["entry_low"] == 1690.0
    assert payload["entry_high"] == 1705.0
    assert payload["stop_loss"] == 1620.0
    assert payload["target_price"] == 1880.0


def test_build_payload_skips_ambiguous_action_non_stock_and_unknown_market() -> None:
    ambiguous = _result(operation_advice="买盘增强，继续观察", action=None)
    assert build_decision_signal_payload_from_report(
        ambiguous,
        trace_id="trace-1",
        query_source="api",
        report_type="simple",
    ) is None

    market_review = _result(operation_advice="买入", action="buy")
    assert build_decision_signal_payload_from_report(
        market_review,
        trace_id="trace-2",
        query_source="api",
        report_type="market_review",
    ) is None

    unknown_market = _result(code="UNKNOWN", operation_advice="买入", action="buy")
    assert build_decision_signal_payload_from_report(
        unknown_market,
        trace_id="trace-3",
        query_source="api",
        report_type="simple",
    ) is None


def test_extract_and_persist_reuses_service_dedup_and_sanitization(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    result = _result(
        analysis_summary="趋势确认 token=super-secret",
    )

    first = extract_and_persist_from_analysis_result(
        result,
        context_snapshot={"market_phase_summary": {"phase": "intraday"}},
        source_report_id=901,
        trace_id="trace-901",
        query_source="api",
        report_type="full",
        service=service,
    )
    second = extract_and_persist_from_analysis_result(
        result,
        context_snapshot={"market_phase_summary": {"phase": "intraday"}},
        source_report_id=901,
        trace_id="trace-901",
        query_source="api",
        report_type="full",
        service=service,
    )

    assert first is not None
    assert second is not None
    assert first["created"] is True
    assert second["created"] is False
    assert first["item"]["reason"] == "趋势确认 token=[REDACTED]"
    assert first["item"]["plan_quality"] == "complete"
    assert first["item"]["horizon"] == "intraday"
    assert first["item"]["expires_at"] is not None

    listed = service.list_signals(source_report_id=901)
    assert listed["total"] == 1
    persisted = listed["items"][0]
    assert persisted["source_report_id"] == 901
    assert persisted["reason"] == "趋势确认 token=[REDACTED]"
    assert persisted["entry_low"] == 1690.0
    assert persisted["entry_high"] == 1700.0


def test_extract_and_persist_missing_price_plan_does_not_fabricate_fields(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    result = _result()
    result.dashboard = {"battle_plan": {"sniper_points": {}}, "intelligence": {}}

    created = extract_and_persist_from_analysis_result(
        result,
        context_snapshot={"market_phase_summary": {"phase": "postmarket"}},
        source_report_id=902,
        trace_id="trace-902",
        query_source="schedule",
        report_type="simple",
        service=service,
    )

    assert created is not None
    item = created["item"]
    assert item["plan_quality"] == "minimal"
    assert item["horizon"] == "3d"
    assert item["expires_at"] is not None
    assert item["entry_low"] is None
    assert item["entry_high"] is None
    assert item["stop_loss"] is None
    assert item["target_price"] is None


def test_crypto_plan_freeze_archives_same_direction_candidate() -> None:
    class Service:
        def get_latest_active(self, **_kwargs):
            return {
                "items": [
                    {
                        "id": 7,
                        "action": "buy",
                        "horizon": "intraday",
                        "created_at": "2026-07-22T00:00:00",
                        "expires_at": "2026-07-22T04:00:00",
                    }
                ]
            }

        def update_status(self, *_args, **_kwargs):
            raise AssertionError("same-direction freeze must not mutate the active plan")

    payload = {
        "market": "crypto",
        "stock_code": "BTC",
        "action": "buy",
        "horizon": "intraday",
        "metadata": {"strategy_plan": {"entry_price": 67000}},
    }

    frozen = _apply_crypto_plan_freeze(payload, Service())

    assert frozen["status"] == "archived"
    assert frozen["metadata"]["plan_lifecycle"] == {
        "state": "superseded_candidate",
        "frozen_by_signal_id": 7,
        "frozen_until": "2026-07-22T04:00:00",
        "reason": "active_plan_still_valid",
    }


def test_crypto_plan_freeze_allows_direction_reversal() -> None:
    class Service:
        def get_latest_active(self, **_kwargs):
            return {
                "items": [
                    {
                        "id": 7,
                        "action": "buy",
                        "horizon": "intraday",
                        "created_at": "2026-07-22T00:00:00",
                        "expires_at": "2026-07-22T04:00:00",
                    }
                ]
            }

        def update_status(self, *_args, **_kwargs):
            raise AssertionError("direction reversal is handled after create")

    payload = {
        "market": "crypto",
        "stock_code": "BTC",
        "action": "sell",
        "horizon": "intraday",
    }

    assert _apply_crypto_plan_freeze(payload, Service()) == payload


def test_wait_plan_does_not_publish_fake_entry_range() -> None:
    result = _result(operation_advice="观望", decision_type="hold")
    result.dashboard["battle_plan"]["intraday_plan"] = {
        "enabled": False,
        "direction": "wait",
        "entry_zone": "66000-67000",
        "no_trade_reason": "等待确认",
    }

    payload = build_decision_signal_payload_from_report(
        result,
        context_snapshot={"analysis_mode": "hourly"},
        source_report_id=903,
        trace_id="trace-903",
        query_source="schedule",
        report_type="simple",
    )

    assert payload is not None
    assert "entry_low" not in payload
    assert "entry_high" not in payload


def test_terminal_crypto_observation_invalidates_stale_actionable_plan(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    ready = _result()
    ready.dashboard["battle_plan"]["intraday_plan"] = {
        "enabled": True,
        "direction": "long",
        "entry_zone": "1710-1720",
        "entry_price": 1715,
        "stop_loss": 1688,
        "take_profit": 1760,
        "trigger_execution_state": "selloff_rebound_trial_ready",
    }
    first = extract_and_persist_from_analysis_result(
        ready,
        context_snapshot={"analysis_mode": "hourly"},
        source_report_id=904,
        trace_id="trace-ready",
        query_source="btc_volatility",
        report_type="simple",
        service=service,
    )
    assert first is not None
    stale_id = first["item"]["id"]

    missed = _result(operation_advice="观望", decision_type="hold", action="watch")
    missed.dashboard["battle_plan"]["intraday_plan"] = {
        "enabled": False,
        "direction": "wait",
        "entry_zone": "1710-1720",
        "no_trade_reason": "本轮反弹已错过",
        "trigger_execution_state": "selloff_rebound_missed",
    }
    second = extract_and_persist_from_analysis_result(
        missed,
        context_snapshot={"analysis_mode": "hourly"},
        source_report_id=905,
        trace_id="trace-missed",
        query_source="hourly_schedule",
        report_type="simple",
        service=service,
    )

    assert second is not None
    assert second["item"]["action"] == "watch"
    assert service.get_signal(stale_id)["status"] == "invalidated"


def test_crypto_risk_guard_downgrades_new_actionable_plan(isolated_db, monkeypatch) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    existing = service.create_signal(
        {
            "stock_code": "BTC",
            "stock_name": "Bitcoin",
            "market": "crypto",
            "source_type": "analysis",
            "source_report_id": 906,
            "trace_id": "trace-risk-existing",
            "trigger_source": "btc_volatility",
            "action": "buy",
            "horizon": "intraday",
            "entry_low": 100,
            "entry_high": 101,
            "stop_loss": 95,
            "target_price": 110,
        }
    )["item"]
    monkeypatch.setattr(
        decision_signal_extractor_module,
        "_crypto_backtest_risk_guard",
        lambda **_kwargs: {
            "reason": "consecutive_loss_cooldown",
            "current_consecutive_losses": 3,
            "thresholds": {"consecutive_loss_cooldown": 3},
        },
    )

    guarded = _apply_crypto_plan_freeze(
        {
            "market": "crypto",
            "stock_code": "BTC",
            "action": "buy",
            "action_label": "买入",
            "horizon": "intraday",
            "report_language": "zh",
            "metadata": {"plan_key": "crypto:BTC:intraday"},
        },
        service,
    )

    assert guarded["action"] == "watch"
    assert guarded["action_label"] == "观望"
    assert guarded["metadata"]["plan_lifecycle"]["state"] == "risk_guarded"
    assert service.get_signal(existing["id"])["status"] == "invalidated"


def test_daily_and_hourly_reports_keep_separate_plan_identities() -> None:
    result = _result()
    result.dashboard["battle_plan"].update(
        {
            "long_plan": {
                "direction": "long",
                "entry_price": "66000",
                "stop_loss": "65000",
                "take_profit": "68000",
                "reason": "日线趋势向上",
            },
            "short_plan": {
                "direction": "short",
                "entry_price": "64000",
                "stop_loss": "65000",
                "take_profit": "62000",
            },
            "intraday_plan": {
                "enabled": True,
                "direction": "long",
                "entry_zone": "65500-65700",
                "stop_loss": "65100",
                "take_profit": "66200",
            },
        }
    )

    daily = build_decision_signal_payload_from_report(
        result,
        context_snapshot={"analysis_mode": "daily"},
        source_report_id=910,
        trace_id="trace-plan-daily",
        query_source="daily_schedule",
        report_type="simple",
    )
    hourly = build_decision_signal_payload_from_report(
        result,
        context_snapshot={"analysis_mode": "hourly"},
        source_report_id=911,
        trace_id="trace-plan-hourly",
        query_source="hourly_schedule",
        report_type="simple",
    )

    assert daily is not None and hourly is not None
    assert daily["horizon"] == "3d"
    assert daily["metadata"]["strategy_plan"]["source"] == "long_plan"
    assert daily["metadata"]["plan_key"] == "crypto:BTC:3d"
    assert hourly["horizon"] == "intraday"
    assert hourly["metadata"]["strategy_plan"]["source"] == "intraday_plan"
    assert hourly["metadata"]["plan_key"] == "crypto:BTC:intraday"


def test_hourly_wait_plan_does_not_fall_through_to_daily_plan() -> None:
    result = _result(operation_advice="观望", decision_type="hold", action="watch")
    result.dashboard["battle_plan"].update(
        {
            "long_plan": {
                "direction": "long",
                "entry_price": "66000",
                "stop_loss": "65000",
                "take_profit": "68000",
            },
            "intraday_plan": {
                "enabled": False,
                "direction": "wait",
                "entry_zone": "65500-65700",
                "no_trade_reason": "等待小时线确认",
            },
        }
    )

    payload = build_decision_signal_payload_from_report(
        result,
        context_snapshot={"analysis_mode": "hourly"},
        source_report_id=912,
        trace_id="trace-hourly-wait",
        query_source="hourly_schedule",
        report_type="simple",
    )

    assert payload is not None
    assert payload["action"] == "watch"
    assert payload["horizon"] == "intraday"
    assert payload["metadata"]["strategy_plan"]["source"] == "intraday_plan"
    assert payload["metadata"]["plan_key"] == "crypto:BTC:intraday"
    assert "entry_low" not in payload
    assert "entry_high" not in payload


def test_daily_neutral_report_does_not_choose_one_side_of_two_way_plan() -> None:
    result = _result(operation_advice="观望", decision_type="hold", action="watch")
    result.dashboard["battle_plan"].update(
        {
            "long_plan": {
                "direction": "long",
                "entry_price": "66000",
                "stop_loss": "65000",
                "take_profit": "68000",
            },
            "short_plan": {
                "direction": "short",
                "entry_price": "64000",
                "stop_loss": "65000",
                "take_profit": "62000",
            },
        }
    )

    payload = build_decision_signal_payload_from_report(
        result,
        context_snapshot={"analysis_mode": "daily"},
        source_report_id=918,
        trace_id="trace-daily-neutral",
        query_source="daily_schedule",
        report_type="simple",
    )

    assert payload is not None
    assert payload["action"] == "watch"
    assert payload["horizon"] == "3d"
    assert payload["metadata"]["plan_key"] == "crypto:BTC:3d"
    assert "strategy_plan" not in payload["metadata"]
    assert "entry_low" not in payload
    assert "entry_high" not in payload


def test_crypto_plan_observation_updates_one_stable_active_plan(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    first_result = _result()
    first = extract_and_persist_from_analysis_result(
        first_result,
        context_snapshot={"analysis_mode": "hourly"},
        source_report_id=904,
        trace_id="trace-plan-first",
        query_source="btc_volatility",
        report_type="simple",
        service=service,
    )
    assert first is not None and first["created"] is True

    second_result = _result(sentiment_score=64, analysis_summary="后续观察更新")
    second = extract_and_persist_from_analysis_result(
        second_result,
        context_snapshot={"analysis_mode": "hourly"},
        source_report_id=905,
        trace_id="trace-plan-second",
        query_source="hourly_schedule",
        report_type="simple",
        service=service,
    )

    assert second is not None
    assert second["created"] is False
    assert second.get("updated") is True
    assert second["item"]["id"] == first["item"]["id"]
    assert second["item"]["source_report_id"] == 904
    assert second["item"]["metadata"]["latest_observation"]["source_report_id"] == 905
    active = service.get_latest_active(stock_code="BTC", market="crypto", limit=10)
    assert [item["id"] for item in active["items"]] == [first["item"]["id"]]


def test_crypto_wait_observation_does_not_downgrade_actionable_plan(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    first = extract_and_persist_from_analysis_result(
        _result(),
        context_snapshot={"analysis_mode": "hourly"},
        source_report_id=906,
        trace_id="trace-plan-actionable",
        query_source="btc_volatility",
        report_type="simple",
        service=service,
    )
    assert first is not None

    wait_result = _result(operation_advice="观望", decision_type="hold", action="watch")
    wait = extract_and_persist_from_analysis_result(
        wait_result,
        context_snapshot={"analysis_mode": "hourly"},
        source_report_id=907,
        trace_id="trace-plan-wait",
        query_source="hourly_schedule",
        report_type="simple",
        service=service,
    )

    assert wait is not None and wait.get("updated") is True
    assert wait["item"]["id"] == first["item"]["id"]
    assert wait["item"]["action"] == "buy"
    assert wait["item"]["entry_low"] == first["item"]["entry_low"]


def test_crypto_avoid_observation_does_not_replace_executable_action(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    first = extract_and_persist_from_analysis_result(
        _result(),
        context_snapshot={"analysis_mode": "hourly"},
        source_report_id=913,
        trace_id="trace-plan-before-avoid",
        query_source="hourly_schedule",
        report_type="simple",
        service=service,
    )
    assert first is not None

    observed = extract_and_persist_from_analysis_result(
        _result(operation_advice="回避", decision_type="hold", action="avoid"),
        context_snapshot={"analysis_mode": "hourly"},
        source_report_id=914,
        trace_id="trace-plan-avoid",
        query_source="hourly_schedule",
        report_type="simple",
        service=service,
    )

    assert observed is not None and observed.get("updated") is True
    assert observed["item"]["id"] == first["item"]["id"]
    assert observed["item"]["action"] == "buy"


def test_crypto_plan_update_archives_legacy_and_keyed_duplicates(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    base_payload = {
        "stock_code": "BTC",
        "stock_name": "Bitcoin",
        "market": "crypto",
        "source_type": "analysis",
        "market_phase": "intraday",
        "trigger_source": "hourly_schedule",
        "action": "buy",
        "horizon": "intraday",
        "entry_low": 65000,
    }
    legacy = service.create_signal(
        {
            **base_payload,
            "source_report_id": 915,
            "trace_id": "trace-plan-legacy",
            "metadata": {"legacy_marker": True},
        }
    )["item"]
    keyed = service.create_signal(
        {
            **base_payload,
            "source_report_id": 916,
            "trace_id": "trace-plan-keyed",
            "metadata": {"plan_key": "crypto:BTC:intraday", "keyed_marker": True},
        }
    )["item"]

    observed = extract_and_persist_from_analysis_result(
        _result(),
        context_snapshot={"analysis_mode": "hourly"},
        source_report_id=917,
        trace_id="trace-plan-converge",
        query_source="hourly_schedule",
        report_type="simple",
        service=service,
    )

    assert observed is not None and observed.get("updated") is True
    assert observed["item"]["id"] == legacy["id"]
    assert observed["item"]["metadata"]["legacy_marker"] is True
    assert observed["item"]["metadata"]["plan_key"] == "crypto:BTC:intraday"
    archived = service.get_signal(keyed["id"])
    assert archived["status"] == "archived"
    assert archived["metadata"]["keyed_marker"] is True
    assert archived["metadata"]["plan_lifecycle"]["canonical_plan_id"] == legacy["id"]


def test_crypto_opposite_direction_is_archived_reversal_candidate(isolated_db) -> None:
    service = DecisionSignalService(db_manager=isolated_db)
    first = extract_and_persist_from_analysis_result(
        _result(),
        context_snapshot={"analysis_mode": "hourly"},
        source_report_id=908,
        trace_id="trace-plan-parent",
        query_source="btc_volatility",
        report_type="simple",
        service=service,
    )
    assert first is not None

    reversal_result = _result(
        operation_advice="卖出",
        decision_type="sell",
        action="sell",
        sentiment_score=38,
    )
    reversal = extract_and_persist_from_analysis_result(
        reversal_result,
        context_snapshot={"analysis_mode": "hourly"},
        source_report_id=909,
        trace_id="trace-plan-reversal",
        query_source="btc_volatility",
        report_type="simple",
        service=service,
    )

    assert reversal is not None
    assert reversal["created"] is True
    assert reversal["item"]["status"] == "archived"
    assert reversal["item"]["metadata"]["plan_lifecycle"]["state"] == "reversal_candidate"
    assert service.get_signal(first["item"]["id"])["status"] == "active"
