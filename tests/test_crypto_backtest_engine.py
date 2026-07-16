# -*- coding: utf-8 -*-
"""Unit tests for BTC plan-level backtest engine."""

import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta

from src.core.crypto_backtest_engine import (
    CryptoBacktestEngine,
    CryptoPlan,
    CryptoPlanBacktestConfig,
)


@dataclass
class Bar:
    timestamp: datetime
    high: float
    low: float
    close: float
    open: float | None = None
    volume: float | None = None
    volume_ratio: float | None = None
    vwap: float | None = None
    execution_open: float | None = None
    execution_high: float | None = None
    execution_low: float | None = None
    execution_close: float | None = None
    mark_open: float | None = None
    mark_high: float | None = None
    mark_low: float | None = None
    mark_close: float | None = None
    funding_rates: tuple[float, ...] = ()
    funding_complete: bool = False


class CryptoBacktestEngineTestCase(unittest.TestCase):
    @staticmethod
    def _contract(*conditions, max_wait_bars=8, max_holding_bars=12):
        return {
            "version": "btc-execution-v1",
            "entry": {
                "logic": "all",
                "conditions": list(conditions),
                "confirmation_bars": 1,
                "fill": "next_bar_open",
                "max_wait_bars": max_wait_bars,
            },
            "exit": {"max_holding_bars": max_holding_bars},
        }

    @classmethod
    def _perpetual_contract(cls, *conditions, margin_mode="isolated", **kwargs):
        contract = cls._contract(*conditions, **kwargs)
        contract["instrument"] = {
            "type": "perpetual",
            "venue": "okx",
            "symbol": "BTC-USDT-PERP",
            "market_symbol": "BTC/USDT:USDT",
            "trigger_price_type": "trade",
            "fill_price_type": "trade",
            "liquidation_price_type": "mark",
            "margin_mode": margin_mode,
        }
        return contract

    @staticmethod
    def _perpetual_bar(timestamp, *, trade_open, trade_high, trade_low, trade_close, mark_high=None, mark_low=None, mark_close=None, funding_rates=()):
        return Bar(
            timestamp=timestamp,
            open=trade_open,
            high=trade_high,
            low=trade_low,
            close=trade_close,
            execution_open=trade_open,
            execution_high=trade_high,
            execution_low=trade_low,
            execution_close=trade_close,
            mark_open=trade_open,
            mark_high=mark_high if mark_high is not None else trade_high,
            mark_low=mark_low if mark_low is not None else trade_low,
            mark_close=mark_close if mark_close is not None else trade_close,
            funding_rates=funding_rates,
            funding_complete=True,
        )

    def _bars(self, highs, lows, closes):
        start = datetime(2026, 1, 1)
        return [
            Bar(timestamp=start + timedelta(hours=i), high=highs[i], low=lows[i], close=closes[i])
            for i in range(len(closes))
        ]

    def test_long_plan_take_profit(self):
        plan = CryptoPlan(
            plan_type="daily_long",
            horizon="daily",
            direction="long",
            entry_price=100,
            stop_loss=95,
            take_profit=108,
            raw_plan={},
        )
        result = CryptoBacktestEngine.evaluate_plan(
            plan=plan,
            forward_bars=self._bars([101, 109], [99, 104], [100, 108]),
            config=CryptoPlanBacktestConfig(neutral_band_pct=0.2),
        )

        self.assertEqual(result["eval_status"], "completed")
        self.assertTrue(result["entry_triggered"])
        self.assertEqual(result["first_hit"], "take_profit")
        self.assertEqual(result["outcome"], "win")
        self.assertAlmostEqual(result["simulated_return_pct"], 1.5709)
        self.assertAlmostEqual(result["diagnostics"]["gross_return_pct"], 8.0)
        self.assertEqual(result["diagnostics"]["trade"]["sizing_method"], "risk")
        self.assertGreater(result["diagnostics"]["trade"]["total_fee"], 0)
        self.assertAlmostEqual(result["diagnostics"]["trade"]["r_multiple"], 1.5709)

    def test_short_plan_take_profit(self):
        plan = CryptoPlan(
            plan_type="daily_short",
            horizon="daily",
            direction="short",
            entry_price=100,
            stop_loss=104,
            take_profit=94,
            raw_plan={},
        )
        result = CryptoBacktestEngine.evaluate_plan(
            plan=plan,
            forward_bars=self._bars([101, 99], [98, 93], [99, 94]),
            config=CryptoPlanBacktestConfig(neutral_band_pct=0.2),
        )

        self.assertEqual(result["eval_status"], "completed")
        self.assertTrue(result["entry_triggered"])
        self.assertEqual(result["first_hit"], "take_profit")
        self.assertEqual(result["outcome"], "win")
        self.assertAlmostEqual(result["simulated_return_pct"], 1.4661)

    def test_summary_includes_portfolio_risk_metrics(self):
        plan = CryptoPlan(
            plan_type="daily_long",
            horizon="daily",
            direction="long",
            entry_price=100,
            stop_loss=95,
            take_profit=108,
            raw_plan={},
        )
        win = CryptoBacktestEngine.evaluate_plan(
            plan=plan,
            forward_bars=self._bars([101, 109], [99, 104], [100, 108]),
            config=CryptoPlanBacktestConfig(neutral_band_pct=0.2),
        )
        loss = CryptoBacktestEngine.evaluate_plan(
            plan=plan,
            forward_bars=self._bars([101, 102], [99, 94], [100, 95]),
            config=CryptoPlanBacktestConfig(neutral_band_pct=0.2),
        )

        class Row:
            def __init__(self, analysis_history_id, payload):
                import json

                self.analysis_history_id = analysis_history_id
                self.analysis_created_at = datetime(2026, 1, analysis_history_id)
                self.plan_type = payload["plan_type"]
                self.eval_status = payload["eval_status"]
                self.entry_triggered = payload["entry_triggered"]
                self.outcome = payload["outcome"]
                self.direction_correct = payload["direction_correct"]
                self.simulated_return_pct = payload["simulated_return_pct"]
                self.diagnostics_json = json.dumps(payload["diagnostics"])

        summary = CryptoBacktestEngine.compute_summary(
            results=[Row(1, win), Row(2, loss)],
            scope="overall",
            code="BTC",
            engine_version="btc-plan-v2",
        )

        self.assertEqual(summary["triggered_count"], 2)
        self.assertEqual(len(summary["equity_curve"]), 2)
        self.assertIn("max_drawdown_pct", summary["risk_metrics"])
        self.assertIn("profit_factor", summary["risk_metrics"])
        self.assertIn("avg_r_multiple", summary["risk_metrics"])
        self.assertGreater(summary["risk_metrics"]["total_fees"], 0)
        self.assertTrue(summary["diagnostics"]["sample_confidence"]["is_low_confidence"])
        self.assertEqual(summary["diagnostics"]["sample_confidence"]["minimum_sample_count"], 30)

    def test_no_entry_is_completed_but_not_direction_scored(self):
        plan = CryptoPlan(
            plan_type="intraday",
            horizon="intraday",
            direction="long",
            entry_price=100,
            stop_loss=95,
            take_profit=105,
            raw_plan={},
        )
        result = CryptoBacktestEngine.evaluate_plan(
            plan=plan,
            forward_bars=self._bars([99, 99.5], [97, 98], [98, 99]),
            config=CryptoPlanBacktestConfig(neutral_band_pct=0.2),
        )

        self.assertEqual(result["eval_status"], "completed")
        self.assertFalse(result["entry_triggered"])
        self.assertEqual(result["outcome"], "no_entry")
        self.assertIsNone(result["direction_correct"])

    def test_v3_requires_close_confirmation_instead_of_high_touch(self):
        plan = CryptoPlan(
            plan_type="intraday",
            horizon="intraday",
            direction="long",
            entry_price=102,
            stop_loss=98,
            take_profit=108,
            raw_plan={},
            execution_contract=self._contract({"type": "close_above", "value": 102}),
        )
        bars = [
            Bar(datetime(2026, 1, 1, 0), open=100, high=105, low=99, close=101),
            Bar(datetime(2026, 1, 1, 1), open=101, high=102, low=99, close=100),
        ]

        result = CryptoBacktestEngine.evaluate_plan(
            plan=plan,
            forward_bars=bars,
            config=CryptoPlanBacktestConfig(engine_version="btc-plan-v3"),
        )

        self.assertEqual(result["eval_status"], "completed")
        self.assertFalse(result["entry_triggered"])
        self.assertEqual(result["simulated_exit_reason"], "conditions_not_met")

    def test_v3_checks_compound_conditions_and_fills_next_bar_open(self):
        plan = CryptoPlan(
            plan_type="intraday",
            horizon="intraday",
            direction="long",
            entry_price=102,
            stop_loss=99,
            take_profit=108,
            raw_plan={},
            execution_contract=self._contract(
                {"type": "close_above", "value": 102},
                {"type": "volume_ratio_gte", "value": 0.5},
                {"type": "close_above_vwap"},
            ),
        )
        bars = [
            Bar(datetime(2026, 1, 1, 0), open=100, high=104, low=99, close=103, volume_ratio=0.6, vwap=101),
            Bar(datetime(2026, 1, 1, 1), open=104, high=109, low=103, close=108, volume_ratio=0.7, vwap=102),
        ]

        result = CryptoBacktestEngine.evaluate_plan(
            plan=plan,
            forward_bars=bars,
            config=CryptoPlanBacktestConfig(engine_version="btc-plan-v3"),
        )

        self.assertTrue(result["entry_triggered"])
        self.assertEqual(result["simulated_entry_price"], 104)
        self.assertEqual(result["entry_triggered_at"], datetime(2026, 1, 1, 1))
        self.assertEqual(result["simulated_exit_reason"], "take_profit")

    def test_v3_open_window_is_provisional_and_not_scored(self):
        plan = CryptoPlan(
            plan_type="intraday",
            horizon="intraday",
            direction="long",
            entry_price=102,
            stop_loss=95,
            take_profit=110,
            raw_plan={},
            execution_contract=self._contract({"type": "close_above", "value": 102}),
        )
        bars = [
            Bar(datetime(2026, 1, 1, 0), open=100, high=104, low=99, close=103),
            Bar(datetime(2026, 1, 1, 1), open=104, high=106, low=103, close=105),
        ]

        result = CryptoBacktestEngine.evaluate_plan(
            plan=plan,
            forward_bars=bars,
            config=CryptoPlanBacktestConfig(engine_version="btc-plan-v3"),
            evaluation_complete=False,
        )

        self.assertEqual(result["eval_status"], "insufficient_data")
        self.assertEqual(result["outcome"], "provisional")
        self.assertIsNone(result["simulated_return_pct"])

    def test_v3_rejects_plan_without_execution_contract(self):
        plan = CryptoPlan(
            plan_type="intraday",
            horizon="intraday",
            direction="long",
            entry_price=100,
            stop_loss=95,
            take_profit=105,
            raw_plan={},
        )

        result = CryptoBacktestEngine.evaluate_plan(
            plan=plan,
            forward_bars=[],
            config=CryptoPlanBacktestConfig(engine_version="btc-plan-v3"),
        )

        self.assertEqual(result["eval_status"], "skipped")
        self.assertEqual(result["diagnostics"]["reason"], "invalid_execution_contract")

    def test_v3_summary_excludes_overlapping_btc_positions(self):
        class Row:
            def __init__(self, history_id, entry_at, exit_at, outcome):
                import json

                self.analysis_history_id = history_id
                self.analysis_created_at = entry_at - timedelta(hours=1)
                self.code = "BTCUSDT"
                self.plan_type = "intraday"
                self.eval_status = "completed"
                self.entry_triggered = True
                self.entry_triggered_at = entry_at
                self.first_hit_at = exit_at
                self.evaluation_end = exit_at
                self.outcome = outcome
                self.direction_correct = outcome == "win"
                self.simulated_return_pct = 1 if outcome == "win" else -1
                pnl = 100 if outcome == "win" else -100
                self.diagnostics_json = json.dumps(
                    {
                        "trade": {
                            "initial_equity": 10000,
                            "net_pnl": pnl,
                            "net_return_pct": self.simulated_return_pct,
                            "r_multiple": self.simulated_return_pct,
                            "total_fee": 2,
                        }
                    }
                )

        start = datetime(2026, 1, 1, 1)
        rows = [
            Row(1, start, start + timedelta(hours=4), "loss"),
            Row(2, start + timedelta(hours=1), start + timedelta(hours=3), "loss"),
            Row(3, start + timedelta(hours=5), start + timedelta(hours=6), "win"),
        ]

        summary = CryptoBacktestEngine.compute_summary(
            results=rows,
            scope="overall",
            code="BTCUSDT",
            engine_version="btc-plan-v3",
        )

        self.assertEqual(summary["triggered_count"], 2)
        self.assertEqual(summary["diagnostics"]["raw_triggered_count"], 3)
        self.assertEqual(summary["diagnostics"]["overlap_excluded_count"], 1)
        self.assertEqual(summary["win_rate_pct"], 50.0)
        self.assertEqual(summary["diagnostics"]["sample_confidence"]["minimum_sample_count"], 100)

    def test_v4_perpetual_deducts_historical_funding(self):
        start = datetime(2026, 1, 1)
        plan = CryptoPlan(
            plan_type="intraday",
            horizon="intraday",
            direction="long",
            entry_price=99,
            stop_loss=90,
            take_profit=110,
            raw_plan={},
            execution_contract=self._perpetual_contract(
                {"type": "close_above", "value": 99},
                max_holding_bars=3,
            ),
        )
        bars = [
            self._perpetual_bar(start, trade_open=99, trade_high=101, trade_low=98, trade_close=100),
            self._perpetual_bar(
                start + timedelta(hours=1),
                trade_open=100,
                trade_high=104,
                trade_low=99,
                trade_close=103,
                funding_rates=(0.001,),
            ),
            self._perpetual_bar(
                start + timedelta(hours=2),
                trade_open=103,
                trade_high=111,
                trade_low=102,
                trade_close=110,
            ),
        ]

        result = CryptoBacktestEngine.evaluate_plan(
            plan=plan,
            forward_bars=bars,
            config=CryptoPlanBacktestConfig(
                engine_version="btc-plan-v4",
                leverage=2,
                slippage_bps=0,
                maker_fee_rate_bps=0,
                taker_fee_rate_bps=0,
            ),
        )

        assert result["simulated_exit_reason"] == "take_profit"
        assert result["diagnostics"]["trade"]["funding_cost"] > 0
        assert result["diagnostics"]["trade"]["net_pnl"] < result["diagnostics"]["trade"]["gross_pnl"]

    def test_v4_perpetual_liquidates_from_mark_price_not_trade_price(self):
        start = datetime(2026, 1, 1)
        plan = CryptoPlan(
            plan_type="intraday",
            horizon="intraday",
            direction="long",
            entry_price=99,
            stop_loss=80,
            take_profit=120,
            raw_plan={},
            execution_contract=self._perpetual_contract({"type": "close_above", "value": 99}),
        )
        bars = [
            self._perpetual_bar(start, trade_open=99, trade_high=101, trade_low=98, trade_close=100),
            self._perpetual_bar(
                start + timedelta(hours=1),
                trade_open=100,
                trade_high=102,
                trade_low=95,
                trade_close=98,
                mark_high=102,
                mark_low=89,
                mark_close=98,
            ),
        ]

        result = CryptoBacktestEngine.evaluate_plan(
            plan=plan,
            forward_bars=bars,
            config=CryptoPlanBacktestConfig(
                engine_version="btc-plan-v4",
                leverage=10,
                slippage_bps=0,
                maker_fee_rate_bps=0,
                taker_fee_rate_bps=0,
            ),
        )

        assert result["simulated_exit_reason"] == "liquidation"
        assert result["first_hit"] == "liquidation"
        assert 90 < result["simulated_exit_price"] < 91

    def test_v4_perpetual_fails_closed_when_mark_data_is_missing(self):
        plan = CryptoPlan(
            plan_type="intraday",
            horizon="intraday",
            direction="long",
            entry_price=99,
            stop_loss=90,
            take_profit=110,
            raw_plan={},
            execution_contract=self._perpetual_contract({"type": "close_above", "value": 99}),
        )
        result = CryptoBacktestEngine.evaluate_plan(
            plan=plan,
            forward_bars=[Bar(datetime(2026, 1, 1), open=100, high=101, low=99, close=100)],
            config=CryptoPlanBacktestConfig(engine_version="btc-plan-v4"),
        )

        assert result["eval_status"] == "insufficient_data"
        assert result["diagnostics"]["reason"] == "incomplete_perpetual_trade_mark_data"


if __name__ == "__main__":
    unittest.main()
