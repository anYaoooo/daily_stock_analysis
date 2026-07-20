# -*- coding: utf-8 -*-
"""Unit tests for BTC plan-level backtest orchestration helpers."""

import unittest
import json
from datetime import datetime, timedelta, timezone

from src.core.crypto_backtest_engine import CryptoPlan
from src.repositories.crypto_backtest_repo import CryptoBacktestRepository
from src.services.crypto_backtest_service import CryptoBacktestService, _Bar
from src.storage import AnalysisHistory, CryptoBacktestResult, DatabaseManager


class CryptoBacktestServiceHelperTestCase(unittest.TestCase):
    def test_bars_from_dataframe_keeps_execution_fields_and_indicators(self):
        import pandas as pd

        bars = CryptoBacktestService._bars_from_dataframe(
            pd.DataFrame(
                [
                    {"date": "2026-01-01 00:00", "open": 99, "high": 101, "low": 98, "close": 100, "volume": 10},
                    {"date": "2026-01-01 01:00", "open": 100, "high": 103, "low": 99, "close": 102, "volume": 20},
                ]
            ),
            period="hourly",
        )

        self.assertEqual(bars[1].open, 100)
        self.assertEqual(bars[1].volume_ratio, 2.0)
        self.assertIsNotNone(bars[1].vwap)

    def test_only_closed_bars_and_mature_windows_are_final(self):
        fetched_at = datetime(2026, 1, 2, 0, 30, tzinfo=timezone.utc)

        self.assertTrue(
            CryptoBacktestService._bar_is_closed(
                datetime(2026, 1, 1), period="daily", fetched_at=fetched_at
            )
        )
        self.assertFalse(
            CryptoBacktestService._bar_is_closed(
                datetime(2026, 1, 2), period="daily", fetched_at=fetched_at
            )
        )
        self.assertFalse(
            CryptoBacktestService._evaluation_window_complete(
                window_end=datetime(2026, 1, 3),
                data_snapshot={"fetched_at": fetched_at.isoformat()},
            )
        )

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

    def test_loss_review_separates_cost_loss_from_direction_mismatch(self):
        cost_loss = CryptoBacktestResult(
            analysis_history_id=71,
            code="BTCUSDT",
            plan_type="intraday",
            horizon="intraday",
            direction="long",
            engine_version="btc-plan-v5",
            eval_status="completed",
            entry_triggered=True,
            outcome="neutral",
            simulated_return_pct=-0.03,
            diagnostics_json=json.dumps(
                {
                    "trade": {"gross_pnl": 1.2, "net_pnl": -0.3, "total_fee": 1.4, "funding_cost": 0.1},
                    "indicator_tags": {"volume": {"confirmation": "low"}},
                }
            ),
        )
        direction_loss = CryptoBacktestResult(
            analysis_history_id=72,
            code="BTCUSDT",
            plan_type="daily_long",
            horizon="daily",
            direction="long",
            engine_version="btc-plan-v5",
            eval_status="completed",
            entry_triggered=True,
            outcome="loss",
            direction_correct=False,
            simulated_return_pct=-1.5,
            simulated_exit_reason="window_end",
            diagnostics_json=json.dumps(
                {"indicator_tags": {"ema": {"structure": "mixed"}}}
            ),
        )

        class Repo:
            def list_results(self, **kwargs):
                self.kwargs = kwargs
                return [cost_loss, direction_loss]

        service = CryptoBacktestService.__new__(CryptoBacktestService)
        service.repo = Repo()
        service._engine_version = lambda: "btc-plan-v5"

        review = service.get_loss_review(code="BTC", limit=10)

        self.assertEqual(review["engine_version"], "btc-plan-v5")
        self.assertEqual(review["reviewed_results"], 2)
        self.assertEqual(review["loss_count"], 2)
        self.assertEqual(review["cause_breakdown"], {
            "costs_exceeded_gross_profit": 1,
            "direction_mismatch": 1,
        })
        self.assertEqual(review["items"][0]["primary_cause"], "costs_exceeded_gross_profit")
        self.assertEqual(review["items"][1]["primary_cause"], "direction_mismatch")
        self.assertIn("不能归因于外部因素", review["items"][1]["external_context"])
        self.assertIn("low", {item["key"] for item in review["indicator_patterns"]})
        self.assertEqual(service.repo.kwargs["engine_version"], "btc-plan-v5")
        self.assertTrue(service.repo.kwargs["net_loss_only"])
        self.assertEqual(service.repo.kwargs["limit"], 10)

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
                                "execution_contract": {
                                    "version": "btc-execution-v1",
                                    "entry": {
                                        "logic": "all",
                                        "conditions": [{"type": "close_above", "value": 100500}],
                                        "confirmation_bars": 1,
                                        "fill": "next_bar_open",
                                        "max_wait_bars": 8,
                                    },
                                    "exit": {"max_holding_bars": 12},
                                },
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
        self.assertEqual(plans[0].execution_contract["version"], "btc-execution-v1")

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
        self.assertEqual(
            item["plans"][0]["missing_fields"],
            ["stop_loss", "take_profit", "execution_contract"],
        )
        self.assertEqual(item["plans"][0]["no_trade_reason"], "等待止损和目标价补齐")

    def test_history_record_marks_explicit_wait_as_no_trade_plan(self):
        analysis = AnalysisHistory(
            id=13,
            query_id="q-13",
            code="BTC",
            raw_result=json.dumps(
                {
                    "dashboard": {
                        "battle_plan": {
                            "intraday_plan": {
                                "enabled": False,
                                "direction": "wait",
                                "no_trade_reason": "小时线结构尚未确认",
                            }
                        }
                    }
                }
            ),
            context_snapshot=json.dumps({"analysis_mode": "hourly"}),
            created_at=datetime(2026, 1, 1, 8),
        )
        service = CryptoBacktestService.__new__(CryptoBacktestService)

        item = service._history_record_to_dict(analysis, {})

        plan = item["plans"][0]
        self.assertEqual(plan["direction"], "wait")
        self.assertEqual(plan["quality_status"], "no_trade_plan")
        self.assertEqual(plan["missing_fields"], [])
        self.assertEqual(plan["backtest_status"], "skipped")
        self.assertFalse(plan["backtestable"])
        self.assertEqual(item["backtest_status"], "skipped")

    def test_extract_plans_respects_daily_wait_direction(self):
        analysis = AnalysisHistory(
            code="BTC",
            raw_result=json.dumps(
                {
                    "dashboard": {
                        "battle_plan": {
                            "long_plan": {
                                "direction": "wait",
                                "no_trade_reason": "风险收益不符合执行标准",
                            }
                        }
                    }
                }
            ),
        )
        service = CryptoBacktestService.__new__(CryptoBacktestService)

        plans = service._extract_plans(analysis)

        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].plan_type, "daily_long")
        self.assertEqual(plans[0].direction, "wait")

    def test_history_record_treats_disabled_directional_plan_as_no_trade(self):
        analysis = AnalysisHistory(
            id=14,
            query_id="q-14",
            code="BTC",
            raw_result=json.dumps(
                {
                    "dashboard": {
                        "battle_plan": {
                            "intraday_plan": {
                                "enabled": False,
                                "direction": "long",
                                "no_trade_reason": "波动条件不足",
                            }
                        }
                    }
                }
            ),
            context_snapshot=json.dumps({"analysis_mode": "hourly"}),
            created_at=datetime(2026, 1, 1, 8),
        )
        service = CryptoBacktestService.__new__(CryptoBacktestService)

        item = service._history_record_to_dict(analysis, {})

        plan = item["plans"][0]
        self.assertEqual(plan["direction"], "wait")
        self.assertEqual(plan["quality_status"], "no_trade_plan")
        self.assertEqual(plan["missing_fields"], [])

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
                                "execution_contract": {
                                    "version": "btc-execution-v1",
                                    "entry": {
                                        "logic": "all",
                                        "conditions": [{"type": "close_above", "value": 100000}],
                                        "confirmation_bars": 1,
                                        "fill": "next_bar_open",
                                        "max_wait_bars": 8,
                                    },
                                    "exit": {"max_holding_bars": 12},
                                },
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
            engine_version="btc-plan-v3",
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
        self.assertEqual(item["plans"][0]["execution_contract"]["version"], "btc-execution-v1")
        self.assertEqual(item["plans"][0]["risk_reward"], "1:2")
        self.assertEqual(item["plans"][0]["latest_result"]["trade"]["net_pnl"], 12.3)

    def test_indicator_tags_are_extracted_from_snapshot_for_intraday_plan(self):
        analysis = AnalysisHistory(
            id=31,
            code="BTC",
            context_snapshot=json.dumps(
                {
                    "enhanced_context": {
                        "crypto_technical": {
                            "timeframes": {
                                "daily": {
                                    "price_action": {"state": "range"},
                                    "ema": {"structure": "mixed"},
                                    "vwap": {"price_position": "below"},
                                    "volume": {"confirmation": "normal"},
                                    "volatility": {"atr14_pct": 2.1},
                                },
                                "hourly": {
                                    "price_action": {"state": "breakout"},
                                    "ema": {"structure": "bullish"},
                                    "vwap": {"price_position": "above"},
                                    "volume": {"confirmation": "high"},
                                    "volatility": {"atr14_pct": 0.8},
                                    "event": {"type": "liquidity_sweep_low_reversal_candidate"},
                                },
                            },
                            "intraday": {
                                "alignment": "countertrend_long",
                                "daily_bias": "short",
                                "hourly_bias": "long",
                            },
                            "derivatives": {
                                "data_quality": "available",
                                "funding": {
                                    "state": "positive_crowded",
                                    "rate_pct": 0.063,
                                },
                                "open_interest": {
                                    "state": "high_notional",
                                },
                                "leverage_pressure": "long_crowding_risk",
                            },
                        }
                    }
                }
            ),
        )
        plan = CryptoPlan(
            plan_type="intraday",
            horizon="intraday",
            direction="long",
            entry_price=100,
            stop_loss=99,
            take_profit=102,
            raw_plan={},
        )

        tags = CryptoBacktestService._indicator_tags_from_snapshot(analysis, plan)

        self.assertEqual(tags["source_timeframe"], "hourly")
        self.assertEqual(tags["price_action"]["state"], "breakout")
        self.assertEqual(tags["ema"]["structure"], "bullish")
        self.assertEqual(tags["vwap"]["price_position"], "above")
        self.assertEqual(tags["volume"]["confirmation"], "high")
        self.assertEqual(tags["volatility"]["atr14_pct"], 0.8)
        self.assertEqual(tags["intraday"]["alignment"], "countertrend_long")
        self.assertEqual(tags["derivatives"]["data_quality"], "available")
        self.assertEqual(tags["derivatives"]["funding_state"], "positive_crowded")
        self.assertEqual(tags["derivatives"]["funding_rate_pct"], 0.063)
        self.assertEqual(tags["derivatives"]["open_interest_state"], "high_notional")
        self.assertEqual(tags["derivatives"]["leverage_pressure"], "long_crowding_risk")
        self.assertEqual(tags["event"]["type"], "liquidity_sweep_low_reversal_candidate")

    def test_indicator_group_breakdown_groups_by_existing_tags(self):
        rows = [
            CryptoBacktestResult(
                analysis_history_id=41,
                code="BTCUSDT",
                plan_type="daily_long",
                horizon="daily",
                direction="long",
                engine_version="btc-plan-v2",
                eval_status="completed",
                entry_triggered=True,
                outcome="win",
                simulated_return_pct=1.2,
                diagnostics_json=json.dumps(
                    {
                        "indicator_tags": {
                            "price_action": {"state": "breakout"},
                            "ema": {"structure": "bullish"},
                        },
                        "trade": {
                            "initial_equity": 10000,
                            "net_pnl": 120,
                            "net_return_pct": 1.2,
                            "r_multiple": 1.2,
                            "total_fee": 2,
                        },
                    }
                ),
            ),
            CryptoBacktestResult(
                analysis_history_id=42,
                code="BTCUSDT",
                plan_type="daily_short",
                horizon="daily",
                direction="short",
                engine_version="btc-plan-v2",
                eval_status="completed",
                entry_triggered=True,
                outcome="loss",
                simulated_return_pct=-0.8,
                diagnostics_json=json.dumps(
                    {
                        "indicator_tags": {
                            "price_action": {"state": "breakdown"},
                            "ema": {"structure": "bearish"},
                        },
                        "trade": {
                            "initial_equity": 10000,
                            "net_pnl": -80,
                            "net_return_pct": -0.8,
                            "r_multiple": -0.8,
                            "total_fee": 2,
                        },
                    }
                ),
            ),
        ]

        breakdown = CryptoBacktestService._indicator_group_breakdown(rows)

        price_action_groups = {
            item["key"]: item for item in breakdown["groups"]["price_action.state"]
        }
        self.assertEqual(price_action_groups["breakout"]["total_evaluations"], 1)
        self.assertEqual(price_action_groups["breakout"]["win_rate_pct"], 100.0)
        self.assertTrue(price_action_groups["breakout"]["sample_confidence"]["is_low_confidence"])
        self.assertEqual(price_action_groups["breakdown"]["win_rate_pct"], 0.0)

    def test_history_record_filters_plans_by_direction_plan_type_and_status(self):
        analysis = AnalysisHistory(
            id=51,
            query_id="q-51",
            code="BTC",
            raw_result=json.dumps(
                {
                    "dashboard": {
                        "battle_plan": {
                            "long_plan": {
                                "entry_price": "100000",
                                "stop_loss": "99000",
                                "take_profit": "102000",
                            },
                            "short_plan": {
                                "entry_price": "98000",
                                "stop_loss": "99000",
                                "take_profit": "95000",
                            },
                        }
                    }
                }
            ),
            created_at=datetime(2026, 1, 1, 8),
        )
        result = CryptoBacktestResult(
            analysis_history_id=51,
            code="BTCUSDT",
            plan_type="daily_short",
            horizon="daily",
            direction="short",
            engine_version="btc-plan-v2",
            eval_status="completed",
            outcome="loss",
        )
        service = CryptoBacktestService.__new__(CryptoBacktestService)
        service._engine_version = lambda: "btc-plan-v2"

        item = service._history_record_to_dict(
            analysis,
            {(51, "daily_short"): result},
            direction_filter="short",
            plan_type_filter="daily_short",
            result_status_filter="loss",
        )

        self.assertIsNotNone(item)
        self.assertEqual(len(item["plans"]), 1)
        self.assertEqual(item["plans"][0]["plan_type"], "daily_short")
        self.assertEqual(item["plans"][0]["backtest_status"], "loss")

    def test_get_history_record_loads_one_record_with_latest_results(self):
        analysis = AnalysisHistory(
            id=21,
            query_id="q-21",
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
            def __init__(self):
                self.history_calls = []
                self.result_calls = []

            def get_history_records(self, *, ids=None, code=None, offset=0, limit=20):
                self.history_calls.append((ids, offset, limit))
                return [analysis], 1

            def get_results_for_history_ids(self, *, analysis_history_ids, engine_version):
                self.result_calls.append((analysis_history_ids, engine_version))
                return [
                    CryptoBacktestResult(
                        analysis_history_id=21,
                        code="BTCUSDT",
                        plan_type="daily_long",
                        horizon="daily",
                        direction="long",
                        engine_version=engine_version,
                        eval_status="completed",
                        outcome="win",
                    )
                ]

        repo = Repo()
        service = CryptoBacktestService.__new__(CryptoBacktestService)
        service.repo = repo
        service._engine_version = lambda: "btc-plan-v2"

        item = service.get_history_record(21)

        self.assertIsNotNone(item)
        self.assertEqual(repo.history_calls, [([21], 0, 1)])
        self.assertEqual(repo.result_calls, [([21], "btc-plan-v2")])
        self.assertEqual(item["analysis_history_id"], 21)
        self.assertEqual(item["plans"][0]["backtest_status"], "win")

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


class CryptoBacktestRepositoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        DatabaseManager.reset_instance()
        self.db = DatabaseManager("sqlite:///:memory:")
        self.repo = CryptoBacktestRepository(self.db)

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()

    def test_net_loss_filter_is_applied_before_limit(self):
        def result(
            analysis_history_id: int,
            created_at: datetime,
            *,
            eval_status: str,
            entry_triggered: bool,
            outcome: str | None,
            simulated_return_pct: float | None,
        ) -> CryptoBacktestResult:
            return CryptoBacktestResult(
                analysis_history_id=analysis_history_id,
                code="BTCUSDT",
                analysis_created_at=created_at,
                evaluated_at=created_at,
                plan_type="daily_long",
                horizon="daily",
                direction="long",
                engine_version="btc-plan-v5",
                eval_status=eval_status,
                entry_triggered=entry_triggered,
                outcome=outcome,
                simulated_return_pct=simulated_return_pct,
            )

        with self.db.get_session() as session:
            session.add_all(
                [
                    result(
                        4,
                        datetime(2026, 1, 4),
                        eval_status="completed",
                        entry_triggered=True,
                        outcome="win",
                        simulated_return_pct=1.0,
                    ),
                    result(
                        3,
                        datetime(2026, 1, 3),
                        eval_status="insufficient_data",
                        entry_triggered=False,
                        outcome=None,
                        simulated_return_pct=None,
                    ),
                    result(
                        2,
                        datetime(2026, 1, 2),
                        eval_status="completed",
                        entry_triggered=True,
                        outcome="neutral",
                        simulated_return_pct=-0.03,
                    ),
                    result(
                        1,
                        datetime(2026, 1, 1),
                        eval_status="completed",
                        entry_triggered=True,
                        outcome="loss",
                        simulated_return_pct=-1.0,
                    ),
                ]
            )
            session.commit()

        rows = self.repo.list_results(
            code="BTCUSDT",
            engine_version="btc-plan-v5",
            net_loss_only=True,
            limit=1,
        )

        self.assertEqual([row.analysis_history_id for row in rows], [2])


if __name__ == "__main__":
    unittest.main()
