# -*- coding: utf-8 -*-
"""Unit tests for BTC plan-level backtest orchestration helpers."""

import unittest
import json
from datetime import datetime, timedelta, timezone

from src.core.crypto_backtest_engine import CryptoPlan
from src.services.crypto_backtest_service import CryptoBacktestService, _Bar
from src.storage import AnalysisHistory, CryptoBacktestResult


class CryptoBacktestServiceHelperTestCase(unittest.TestCase):
    def test_kline_metadata_freezes_source_range_and_hash(self):
        bars = [
            _Bar(timestamp=datetime(2026, 1, 1), high=101, low=99, close=100),
            _Bar(timestamp=datetime(2026, 1, 2), high=106, low=100, close=105),
        ]

        metadata = CryptoBacktestService._kline_metadata(
            code="BTCUSDT",
            period="daily",
            days=5,
            fetched_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
            bars=bars,
        )

        self.assertEqual(metadata["source"], "CryptoFetcher")
        self.assertEqual(metadata["code"], "BTCUSDT")
        self.assertEqual(metadata["period"], "daily")
        self.assertEqual(metadata["bar_count"], 2)
        self.assertEqual(metadata["range_start"], "2026-01-01T00:00:00")
        self.assertEqual(metadata["range_end"], "2026-01-02T00:00:00")
        self.assertTrue(metadata["data_hash"].startswith("sha256:"))

    def test_lookahead_guard_flags_window_or_bars_before_analysis(self):
        analysis_at = datetime(2026, 1, 2, 8)
        guard = CryptoBacktestService._lookahead_guard(
            analysis_at=analysis_at,
            window_start=analysis_at - timedelta(hours=1),
            window_end=analysis_at + timedelta(hours=1),
            forward_bars=[
                _Bar(timestamp=analysis_at - timedelta(minutes=30), high=101, low=99, close=100)
            ],
        )

        self.assertFalse(guard["passed"])
        self.assertIn("window_starts_before_analysis", guard["violations"])
        self.assertIn("bar_before_analysis", guard["violations"])

    def test_attach_run_diagnostics_adds_snapshot_and_lookahead_guard(self):
        plan = CryptoPlan(
            plan_type="daily_long",
            horizon="daily",
            direction="long",
            entry_price=100,
            stop_loss=95,
            take_profit=105,
            raw_plan={},
        )
        evaluation = CryptoBacktestService._skipped_evaluation(plan, "lookahead_bias_detected")

        enriched = CryptoBacktestService._attach_run_diagnostics(
            evaluation=evaluation,
            data_snapshot={"bar_count": 1, "data_hash": "sha256:test"},
            lookahead_guard={"passed": False, "violations": ["bar_before_analysis"]},
        )

        self.assertEqual(enriched["eval_status"], "skipped")
        self.assertEqual(enriched["diagnostics"]["reason"], "lookahead_bias_detected")
        self.assertEqual(enriched["diagnostics"]["data_snapshot"]["data_hash"], "sha256:test")
        self.assertFalse(enriched["diagnostics"]["lookahead_guard"]["passed"])

    def test_delete_result_recomputes_summaries_after_delete(self):
        class Repo:
            def __init__(self):
                self.calls = []

            def delete_result(self, *, analysis_history_id, plan_type, engine_version):
                self.calls.append((analysis_history_id, plan_type, engine_version))
                return 1, "BTCUSDT"

        service = CryptoBacktestService.__new__(CryptoBacktestService)
        service.repo = Repo()
        recomputed = []
        service._recompute_summaries = lambda *, engine_version: recomputed.append(engine_version)
        service._engine_version = lambda: "btc-plan-v2"

        result = service.delete_result(analysis_history_id=10, plan_type="daily_long")

        self.assertEqual(result, {"deleted": 1})
        self.assertEqual(service.repo.calls, [(10, "daily_long", "btc-plan-v2")])
        self.assertEqual(recomputed, ["btc-plan-v2"])

    def test_delete_result_skips_summary_recompute_when_missing(self):
        class Repo:
            def delete_result(self, *, analysis_history_id, plan_type, engine_version):
                return 0, None

        service = CryptoBacktestService.__new__(CryptoBacktestService)
        service.repo = Repo()
        recomputed = []
        service._recompute_summaries = lambda *, engine_version: recomputed.append(engine_version)
        service._engine_version = lambda: "btc-plan-v2"

        result = service.delete_result(analysis_history_id=10, plan_type="daily_long")

        self.assertEqual(result, {"deleted": 0})
        self.assertEqual(recomputed, [])

    def test_extract_plans_for_hourly_report_only_returns_intraday_plan(self):
        analysis = AnalysisHistory(
            code="BTC",
            raw_result=json.dumps(
                {
                    "dashboard": {
                        "battle_plan": {
                            "long_plan": {
                                "entry_price": "100000",
                                "stop_loss": "99000",
                                "take_profit": "103000",
                            },
                            "short_plan": {
                                "entry_price": "98000",
                                "stop_loss": "99000",
                                "take_profit": "95000",
                            },
                            "intraday_plan": {
                                "enabled": True,
                                "direction": "long",
                                "entry_price": "100500",
                                "stop_loss": "100000",
                                "take_profit": "101500",
                            },
                        }
                    }
                }
            ),
            context_snapshot=json.dumps({"analysis_mode": "hourly"}),
        )
        service = CryptoBacktestService.__new__(CryptoBacktestService)

        plans = service._extract_plans(analysis)

        self.assertEqual([plan.plan_type for plan in plans], ["intraday"])
        self.assertEqual(plans[0].direction, "long")

    def test_history_record_marks_missing_plan_fields(self):
        analysis = AnalysisHistory(
            id=11,
            query_id="q-11",
            code="BTC",
            name="Bitcoin",
            raw_result=json.dumps(
                {
                    "dashboard": {
                        "battle_plan": {
                            "long_plan": {
                                "entry_price": "100000",
                                "trigger_condition": "突破确认",
                                "no_trade_reason": "等待止损和目标价补齐",
                            }
                        }
                    }
                }
            ),
            created_at=datetime(2026, 1, 1, 8),
        )
        service = CryptoBacktestService.__new__(CryptoBacktestService)

        item = service._history_record_to_dict(analysis, {})

        self.assertEqual(item["backtest_status"], "invalid_plan")
        self.assertEqual(item["plans"][0]["plan_type"], "daily_long")
        self.assertFalse(item["plans"][0]["backtestable"])
        self.assertEqual(item["plans"][0]["missing_fields"], ["stop_loss", "take_profit"])
        self.assertEqual(item["plans"][0]["no_trade_reason"], "等待止损和目标价补齐")

    def test_history_record_attaches_latest_backtest_result_status(self):
        analysis = AnalysisHistory(
            id=12,
            query_id="q-12",
            code="BTC",
            raw_result=json.dumps(
                {
                    "dashboard": {
                        "battle_plan": {
                            "long_plan": {
                                "entry_price": "100000",
                                "stop_loss": "99000",
                                "take_profit": "102000",
                                "risk_reward": "1:2",
                            }
                        }
                    }
                }
            ),
            created_at=datetime(2026, 1, 1, 8),
        )
        result = CryptoBacktestResult(
            analysis_history_id=12,
            code="BTCUSDT",
            plan_type="daily_long",
            horizon="daily",
            direction="long",
            engine_version="btc-plan-v2",
            eval_status="completed",
            outcome="win",
            entry_triggered=True,
            simulated_return_pct=1.2,
            diagnostics_json=json.dumps({"trade": {"net_pnl": 12.3, "r_multiple": 1.1}}),
        )
        service = CryptoBacktestService.__new__(CryptoBacktestService)

        item = service._history_record_to_dict(analysis, {(12, "daily_long"): result})

        self.assertEqual(item["backtest_status"], "completed")
        self.assertEqual(item["plans"][0]["backtest_status"], "win")
        self.assertEqual(item["plans"][0]["risk_reward"], "1:2")
        self.assertEqual(item["plans"][0]["latest_result"]["trade"]["net_pnl"], 12.3)

    def test_run_selected_skips_existing_plan_without_force(self):
        analysis = AnalysisHistory(
            id=13,
            code="BTC",
            raw_result=json.dumps(
                {
                    "dashboard": {
                        "battle_plan": {
                            "long_plan": {
                                "entry_price": "100000",
                                "stop_loss": "99000",
                                "take_profit": "102000",
                            }
                        }
                    }
                }
            ),
            created_at=datetime(2026, 1, 1, 8),
        )

        class Repo:
            def get_history_records(self, *, ids=None, code=None, offset=0, limit=20):
                return [analysis], 1

            def get_results_for_history_ids(self, *, analysis_history_ids, engine_version):
                return [
                    CryptoBacktestResult(
                        analysis_history_id=13,
                        plan_type="daily_long",
                        engine_version=engine_version,
                    )
                ]

        service = CryptoBacktestService.__new__(CryptoBacktestService)
        service.repo = Repo()
        service._engine_version = lambda: "btc-plan-v2"

        stats = service.run_selected_backtests(analysis_history_ids=[13], force=False)

        self.assertEqual(stats["processed"], 1)
        self.assertEqual(stats["saved"], 0)
        self.assertEqual(stats["skipped"], 1)


if __name__ == "__main__":
    unittest.main()
