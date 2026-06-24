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


class CryptoBacktestEngineTestCase(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
