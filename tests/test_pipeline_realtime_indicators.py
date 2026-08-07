# -*- coding: utf-8 -*-
"""
Issue #234 盘中实时技术指标的单元测试。

覆盖范围：
- _augment_historical_with_realtime：追加/更新逻辑和防护条件
- _compute_ma_status：均线排列文案
- _enhance_context：使用 realtime + trend_result 覆盖 today
"""

import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from data_provider.realtime_types import UnifiedRealtimeQuote, RealtimeSource
from src.stock_analyzer import StockTrendAnalyzer, TrendAnalysisResult, TrendStatus
from src.core.pipeline import StockAnalysisPipeline
from src.analyzer import GeminiAnalyzer


def _make_realtime_quote(
    price: float = 15.72,
    open_price: float = 15.62,
    high: float = 16.29,
    low: float = 15.55,
    volume: int = 13995600,
    amount: float = None,
    change_pct: float = 0.96,
    **overrides,
) -> UnifiedRealtimeQuote:
    return UnifiedRealtimeQuote(
        code="600519",
        name="贵州茅台",
        source=RealtimeSource.TENCENT,
        price=price,
        open_price=open_price,
        high=high,
        low=low,
        volume=volume,
        amount=amount,
        change_pct=change_pct,
        **overrides,
    )


def _make_historical_df(days: int = 25, last_date: date = None) -> pd.DataFrame:
    """构造历史 OHLCV DataFrame。"""
    if last_date is None:
        last_date = date.today() - timedelta(days=1)
    dates = [last_date - timedelta(days=i) for i in range(days - 1, -1, -1)]
    base = 100.0
    data = []
    for i, d in enumerate(dates):
        close = base + i * 0.5
        data.append({
            "code": "600519",
            "date": d,
            "open": close - 0.2,
            "high": close + 0.3,
            "low": close - 0.3,
            "close": close,
            "volume": 1000000 + i * 10000,
            "amount": close * (1000000 + i * 10000),
            "pct_chg": 0.5,
            "ma5": close,
            "ma10": close - 0.1,
            "ma20": close - 0.2,
            "volume_ratio": 1.0,
        })
    return pd.DataFrame(data)


class TestAugmentHistoricalWithRealtime(unittest.TestCase):
    """_augment_historical_with_realtime 的测试。"""

    def setUp(self) -> None:
        self._db_path = os.path.join(
            os.path.dirname(__file__), "..", "data", "test_issue234.db"
        )
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        with patch.dict(os.environ, {"DATABASE_PATH": self._db_path}):
            from src.config import Config
            Config._instance = None
            self.config = Config._load_from_env()
        self.pipeline = StockAnalysisPipeline(config=self.config)

    def test_returns_unchanged_when_realtime_none(self) -> None:
        df = _make_historical_df()
        result = self.pipeline._augment_historical_with_realtime(df, None, "600519")
        self.assertIs(result, df)
        self.assertEqual(len(result), len(df))

    def test_returns_unchanged_when_price_invalid(self) -> None:
        df = _make_historical_df()
        quote = _make_realtime_quote(price=0)
        result = self.pipeline._augment_historical_with_realtime(df, quote, "600519")
        self.assertEqual(len(result), len(df))
        quote2 = MagicMock()
        quote2.price = None
        result2 = self.pipeline._augment_historical_with_realtime(df, quote2, "600519")
        self.assertEqual(len(result2), len(df))

    def test_returns_unchanged_when_df_empty(self) -> None:
        df = pd.DataFrame()
        quote = _make_realtime_quote()
        result = self.pipeline._augment_historical_with_realtime(df, quote, "600519")
        self.assertTrue(result.empty)

    def test_returns_unchanged_when_df_missing_close(self) -> None:
        df = pd.DataFrame({"date": [date.today()], "open": [100]})
        quote = _make_realtime_quote()
        result = self.pipeline._augment_historical_with_realtime(df, quote, "600519")
        self.assertEqual(len(result), 1)
        self.assertNotIn("close", result.columns)

    @patch("src.core.pipeline.get_market_now")
    @patch("src.core.pipeline.is_market_open", return_value=True)
    @patch("src.core.pipeline.get_market_for_stock", return_value="cn")
    def test_appends_row_when_last_date_before_today(
        self, _mock_market, _mock_open, mock_now
    ) -> None:
        today = date.today()
        # 固定市场时钟为 UTC 当日，使 pipeline 的 market_today 等于 date.today()，
        # 不受 get_market_now 通常使用的市场时区影响（例如 CST=UTC+8）。
        mock_now.return_value = datetime(
            today.year, today.month, today.day, 10, 0, tzinfo=timezone.utc
        )
        df = _make_historical_df(last_date=today - timedelta(days=1))
        quote = _make_realtime_quote(price=15.72)
        result = self.pipeline._augment_historical_with_realtime(df, quote, "600519")
        self.assertEqual(len(result), len(df) + 1)
        last = result.iloc[-1]
        self.assertEqual(last["close"], 15.72)
        self.assertEqual(last["date"], today)

    @patch("src.core.pipeline.get_market_now")
    @patch("src.core.pipeline.is_market_open", return_value=True)
    @patch("src.core.pipeline.get_market_for_stock", return_value="cn")
    def test_updates_last_row_when_last_date_is_today(
        self, _mock_market, _mock_open, mock_now
    ) -> None:
        today = date.today()
        # 固定市场时钟为当日，使 last_date >= market_today，从而更新最后一行而不是追加。
        # 这可以避免 CI 在 CST 收盘后运行时出现日期边界偏移。
        mock_now.return_value = datetime(
            today.year, today.month, today.day, 10, 0, tzinfo=timezone.utc
        )
        df = _make_historical_df(last_date=today, days=25)
        df.loc[df.index[-1], "date"] = today
        quote = _make_realtime_quote(price=16.0)
        result = self.pipeline._augment_historical_with_realtime(df, quote, "600519")
        self.assertEqual(len(result), len(df))
        self.assertEqual(result.iloc[-1]["close"], 16.0)


class TestComputeMaStatus(unittest.TestCase):
    """_compute_ma_status 的测试。"""

    def test_bullish_alignment(self) -> None:
        status = StockAnalysisPipeline._compute_ma_status(11, 10, 9.5, 9)
        self.assertIn("多头", status)

    def test_bearish_alignment(self) -> None:
        status = StockAnalysisPipeline._compute_ma_status(8, 9, 9.5, 10)
        self.assertIn("空头", status)

    def test_consolidation(self) -> None:
        status = StockAnalysisPipeline._compute_ma_status(10, 10, 10, 10)
        self.assertIn("震荡", status)


class TestCryptoTechnicalPrompt(unittest.TestCase):
    """BTC 专项交易框架 prompt 注入测试。"""

    def test_prompt_includes_crypto_framework_when_context_present(self) -> None:
        analyzer = GeminiAnalyzer()
        analyzer._get_skill_prompt_sections = lambda: ("", "", False)
        context = {
            "code": "BTC",
            "stock_name": "Bitcoin",
            "date": "2026-06-22",
            "today": {"close": 100000, "volume": 1200},
            "crypto_technical": {
                "price_action": {
                    "state": "breakout",
                    "recent_high": 101000,
                    "recent_low": 95000,
                    "close_change_pct": 2.5,
                    "high_swept": True,
                    "close_above_resistance": True,
                    "low_swept": False,
                    "close_below_support": False,
                },
                "fibonacci": {
                    "swing_high": 101000,
                    "swing_low": 90000,
                    "retracement_levels": {
                        "38.2%": 96798,
                        "50.0%": 95500,
                        "61.8%": 94202,
                    },
                },
                "volume": {
                    "latest": 1200,
                    "average": 1000,
                    "ratio": 1.2,
                    "confirmation": "normal",
                },
                "volatility": {"atr14": 1600, "atr14_pct": 1.6},
                "vwap": {"rolling_20": 98000, "price_position": "above"},
                "ema": {"ema20": 97000, "ema50": 94000, "structure": "bullish"},
            },
        }

        prompt = analyzer._format_prompt(context, "Bitcoin", report_language="zh")

        self.assertIn("BTC 交易框架补充", prompt)
        self.assertIn("Price Action", prompt)
        self.assertIn("扫过前高=True", prompt)
        self.assertIn("收盘站上阻力=True", prompt)
        self.assertIn("流动性掠夺/假突破偏空风险", prompt)
        self.assertIn("Fibonacci", prompt)
        self.assertIn("Volatility / ATR", prompt)
        self.assertIn("ATR14=1600", prompt)
        self.assertIn("反弹/回调或短线推进", prompt)
        self.assertIn("VWAP", prompt)
        self.assertIn("EMA20=97000", prompt)
        self.assertIn("日线 Price Action", prompt)
        self.assertIn("多空共振", prompt)
        self.assertIn("多单", prompt)
        self.assertIn("空单", prompt)
        self.assertIn("做空开仓", prompt)
        self.assertIn("不得只给多单买入视角", prompt)
        self.assertIn("`sell` 仅表示 Short", prompt)
        self.assertIn("`buy` 仅表示 Long", prompt)
        self.assertIn("Flat / 空仓等待", prompt)
        self.assertIn("long_plan", prompt)
        self.assertIn("short_plan", prompt)
        self.assertIn("intraday_plan", prompt)
        self.assertIn("BTC 小时线日内交易机会", prompt)
        self.assertIn("小时线数据缺失", prompt)
        self.assertIn('direction="wait"', prompt)
        self.assertIn("必须同时输出", prompt)
        self.assertNotIn("是否满足 MA5>MA10>MA20 多头排列", prompt)
        self.assertIn("加密货币基础信息", prompt)
        self.assertIn("交易标的", prompt)
        self.assertIn("标的名称", prompt)
        self.assertIn("USDT", prompt)
        self.assertNotIn("股票基础信息", prompt)
        self.assertNotIn("财报与分红", prompt)
        self.assertNotIn("实时行情增强数据", prompt)
        self.assertNotIn("换手率", prompt)
        self.assertNotIn("市盈率", prompt)
        self.assertNotIn("市净率", prompt)
        self.assertNotIn("筹码分布", prompt)
        self.assertNotIn("主力资金流向", prompt)
        self.assertNotIn("筹码结构是否支持", prompt)

    def test_btc_system_prompt_uses_two_way_default_policy(self) -> None:
        analyzer = GeminiAnalyzer(use_legacy_default_prompt=True)

        prompt = analyzer._get_analysis_system_prompt("zh", stock_code="BTC")

        self.assertIn("BTC 默认技能基线", prompt)
        self.assertIn("必须同时评估多单与空单", prompt)
        self.assertNotIn("多头排列必须条件", prompt)
        self.assertNotIn("只做多头排列的股票", prompt)

    def test_prompt_includes_hourly_intraday_framework_when_context_present(self) -> None:
        analyzer = GeminiAnalyzer()
        analyzer._get_skill_prompt_sections = lambda: ("", "", False)
        daily_context = {
            "price_action": {
                "state": "breakout",
                "recent_high": 101000,
                "recent_low": 95000,
                "close_change_pct": 2.5,
            },
            "fibonacci": {
                "swing_high": 101000,
                "swing_low": 90000,
                "retracement_levels": {"38.2%": 96798, "50.0%": 95500, "61.8%": 94202},
            },
            "volume": {"latest": 1200, "average": 1000, "ratio": 1.2, "confirmation": "normal"},
            "volatility": {"atr14": 1600, "atr14_pct": 1.6},
            "vwap": {"rolling_20": 98000, "price_position": "above"},
            "ema": {"ema20": 97000, "ema50": 94000, "structure": "bullish"},
        }
        hourly_context = {
            "price_action": {
                "state": "bullish_push",
                "recent_high": 100800,
                "recent_low": 98700,
                "close_change_pct": 0.8,
            },
            "fibonacci": {
                "swing_high": 100800,
                "swing_low": 98700,
                "retracement_levels": {"38.2%": 99998, "50.0%": 99750, "61.8%": 99502},
            },
            "volume": {"latest": 120, "average": 100, "ratio": 1.2, "confirmation": "normal"},
            "volatility": {"atr14": 420, "atr14_pct": 0.42},
            "vwap": {"rolling_20": 99500, "price_position": "above"},
            "ema": {"ema20": 99200, "ema50": 98900, "structure": "bullish"},
            "event": {
                "type": "selloff_rebound_candidate",
                "suggested_direction": "conditional_long",
                "urgency": "high",
                "reference_high": 100800,
                "event_low": 98700,
                "event_bar_high": 99600,
                "drop_from_reference_high_pct": -2.08,
                "rebound_from_event_low_pct": 0.9,
                "atr_move": 2.4,
                "trigger_reference": {
                    "long_confirmation_price": 99600,
                    "long_invalidation_price": 98574,
                    "short_breakdown_price": 98700,
                },
            },
        }
        context = {
            "code": "BTC",
            "stock_name": "Bitcoin",
            "date": "2026-06-22",
            "today": {"close": 100000, "volume": 1200},
            "analysis_mode": "hourly",
            "trigger_context": {
                "trigger_source": "btc_volatility",
                "trigger_reason": "volatility_spike",
                "direction": "down",
                "change_pct": -1.2,
                "price": 98800,
                "baseline_price": 100000,
                "window_seconds": 120,
                "threshold_pct": 1.0,
                "confirmation_count": 2,
                "confirmation_required": 2,
                "provider_timestamp": "2026-06-22T10:05:00Z",
            },
            "crypto_technical": {
                **daily_context,
                "timeframes": {"daily": daily_context, "hourly": hourly_context},
                "intraday": {
                    "daily_bias": "long",
                    "hourly_bias": "long",
                    "alignment": "aligned_long",
                    "opportunity": "小时线与日线偏多共振，可寻找顺日线的日内多单触发。",
                },
                "derivatives": {
                    "data_quality": "available",
                    "funding": {"rate_pct": 0.063, "state": "positive_crowded"},
                    "open_interest": {
                        "value": 180000,
                        "state": "high_notional",
                        "notional_usdt": 11700000000,
                    },
                    "leverage_pressure": "long_crowding_risk",
                },
            },
        }

        prompt = analyzer._format_prompt(context, "Bitcoin", report_language="zh")

        self.assertIn("BTC 小时线日内交易机会", prompt)
        self.assertIn("独立判断", prompt)
        self.assertIn("日线偏向", prompt)
        self.assertIn("小时线偏向", prompt)
        self.assertIn("aligned_long", prompt)
        self.assertIn("日线偏空但小时线出现多单机会", prompt)
        self.assertIn("逆日线短线计划", prompt)
        self.assertIn("ATR14=420", prompt)
        self.assertIn("止损不得落在小时线常规 ATR 噪音内", prompt)
        self.assertIn("小时线急跌/扫低事件", prompt)
        self.assertIn("selloff_rebound_candidate", prompt)
        self.assertIn("多单确认价=99600", prompt)
        self.assertIn("多单失效价=98574", prompt)
        self.assertIn("空单跌破价=98700", prompt)
        self.assertIn("BTC 急跌机会约束", prompt)
        self.assertIn("衍生品杠杆环境", prompt)
        self.assertIn("资金费率=0.063%", prompt)
        self.assertIn("long_crowding_risk", prompt)
        self.assertIn("Funding/OI 只作为杠杆拥挤度和风控降权信息", prompt)
        self.assertIn("BTC 小时线日内计划强制结构", prompt)
        self.assertIn("dashboard.battle_plan.intraday_plan", prompt)
        self.assertIn("BTC 分步执行要求", prompt)
        self.assertIn("execution_ladder", prompt)
        self.assertIn("试仓", prompt)
        self.assertIn("确认加仓", prompt)
        self.assertIn("liquidity_sweep_reversal", prompt)
        self.assertIn("daily_constraint", prompt)
        self.assertIn("BTC 波动触发上下文", prompt)
        self.assertIn("volatility_spike", prompt)
        self.assertIn("短窗口冲击", prompt)
        self.assertIn("当前可执行", prompt)
        self.assertIn("禁止把虚构的理想回踩价包装成当前建议", prompt)
        self.assertIn("当前 1 小时 K 线可能尚未收线", prompt)
        self.assertIn("2/2", prompt)


class TestEnhanceContextRealtimeOverride(unittest.TestCase):
    """_enhance_context 使用实时行情和趋势结果覆盖 today 的测试。"""

    def setUp(self) -> None:
        self._db_path = os.path.join(
            os.path.dirname(__file__), "..", "data", "test_issue234.db"
        )
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        with patch.dict(os.environ, {"DATABASE_PATH": self._db_path}):
            from src.config import Config
            Config._instance = None
            self.config = Config._load_from_env()
        self.pipeline = StockAnalysisPipeline(config=self.config)

    @patch("src.core.pipeline.get_market_now")
    @patch("src.core.pipeline.get_market_for_stock", return_value="cn")
    def test_today_overridden_when_realtime_and_trend_exist(
        self, _mock_market, mock_now
    ) -> None:
        today = date.today()
        # 固定市场时钟，使 _enhance_context 设置 enhanced['date'] == date.today().isoformat()，
        # 不受 get_market_now 通常使用的市场时区影响（例如 CST=UTC+8）。
        mock_now.return_value = datetime(
            today.year, today.month, today.day, 10, 0, tzinfo=timezone.utc
        )
        context = {
            "code": "600519",
            "date": (today - timedelta(days=1)).isoformat(),
            "today": {"close": 15.0, "ma5": 14.8, "ma10": 14.5},
            "yesterday": {"close": 14.5, "volume": 1000000},
        }
        quote = _make_realtime_quote(price=15.72, volume=2000000)
        trend = TrendAnalysisResult(
            code="600519",
            trend_status=TrendStatus.BULL,
            ma5=15.5,
            ma10=15.2,
            ma20=14.9,
        )
        enhanced = self.pipeline._enhance_context(
            context, quote, None, trend, "贵州茅台"
        )
        self.assertEqual(enhanced["today"]["close"], 15.72)
        self.assertEqual(enhanced["today"]["ma5"], 15.5)
        self.assertEqual(enhanced["today"]["ma10"], 15.2)
        self.assertEqual(enhanced["today"]["ma20"], 14.9)
        self.assertIn("多头", enhanced["ma_status"])
        self.assertEqual(enhanced["date"], today.isoformat())
        self.assertEqual(enhanced["today"]["date"], today.isoformat())
        self.assertEqual(enhanced["today"]["data_source"], "realtime:tencent")
        self.assertEqual(enhanced["today"]["realtime_source"], "tencent")
        self.assertIn("price_change_ratio", enhanced)
        self.assertIn("volume_change_ratio", enhanced)

    @patch("src.core.pipeline.get_market_now")
    @patch("src.core.pipeline.get_market_for_stock", return_value="cn")
    def test_tencent_688691_volume_change_ratio_uses_normalized_share_volume(
        self, _mock_market, mock_now
    ) -> None:
        today = date.today()
        mock_now.return_value = datetime(
            today.year, today.month, today.day, 10, 0, tzinfo=timezone.utc
        )
        context = {
            "code": "688691",
            "date": (today - timedelta(days=1)).isoformat(),
            "today": {
                "close": 128.46,
                "volume": 19512753,
                "amount": 2487341983,
                "date": (today - timedelta(days=1)).isoformat(),
                "dataSource": "AkshareFetcher",
            },
            "yesterday": {"close": 128.46, "volume": 19512753},
        }
        quote = UnifiedRealtimeQuote(
            code="688691",
            name="灿芯股份",
            source=RealtimeSource.TENCENT,
            price=122.70,
            open_price=120.09,
            high=125.96,
            low=116.20,
            volume=10931723,
            amount=1327404280,
            change_pct=3.40,
        )
        trend = TrendAnalysisResult(
            code="688691",
            trend_status=TrendStatus.BULL,
            ma5=120.014,
            ma10=119.425,
            ma20=115.8305,
        )

        enhanced = self.pipeline._enhance_context(
            context, quote, None, trend, "灿芯股份"
        )

        self.assertEqual(enhanced["today"]["volume"], 10931723)
        self.assertEqual(enhanced["today"]["amount"], 1327404280)
        self.assertEqual(enhanced["volume_change_ratio"], 0.56)
        self.assertEqual(enhanced["today"]["date"], today.isoformat())
        self.assertEqual(enhanced["today"]["data_source"], "realtime:tencent")
        self.assertEqual(enhanced["today"]["realtime_source"], "tencent")
        self.assertNotIn("dataSource", enhanced["today"])

    @patch("src.core.pipeline.get_market_now")
    @patch("src.core.pipeline.get_market_for_stock", return_value="cn")
    def test_realtime_metadata_and_partial_estimated_fields_are_propagated(
        self, _mock_market, mock_now
    ) -> None:
        today = date.today()
        mock_now.return_value = datetime(
            today.year, today.month, today.day, 10, 0, tzinfo=timezone.utc
        )
        context = {
            "code": "600519",
            "date": (today - timedelta(days=1)).isoformat(),
            "today": {
                "close": 15.0,
                "amount": 999999,
                "date": (today - timedelta(days=1)).isoformat(),
                "dataSource": "AkshareFetcher",
            },
            "yesterday": {"close": 14.5, "volume": 1000000},
        }
        quote = _make_realtime_quote(
            price=15.72,
            amount=None,
            fetched_at="2026-05-31T10:00:05+00:00",
            provider_timestamp="2026-05-31T10:00:00+00:00",
            is_stale=False,
            stale_seconds=5,
            fallback_from="efinance",
        )
        trend = TrendAnalysisResult(
            code="600519",
            trend_status=TrendStatus.BULL,
            ma5=15.5,
            ma10=15.2,
            ma20=14.9,
        )

        enhanced = self.pipeline._enhance_context(
            context,
            quote,
            None,
            trend,
            "贵州茅台",
            market_phase_context={"is_partial_bar": True},
        )

        self.assertEqual(enhanced["realtime"]["source"], "tencent")
        self.assertEqual(enhanced["realtime"]["fetched_at"], "2026-05-31T10:00:05+00:00")
        self.assertEqual(enhanced["realtime"]["provider_timestamp"], "2026-05-31T10:00:00+00:00")
        self.assertIs(enhanced["realtime"]["is_stale"], False)
        self.assertEqual(enhanced["realtime"]["stale_seconds"], 5)
        self.assertEqual(enhanced["realtime"]["fallback_from"], "efinance")
        self.assertTrue(enhanced["today"]["is_partial_bar"])
        self.assertTrue(enhanced["today"]["is_estimated"])
        self.assertEqual(
            enhanced["today"]["estimated_fields"],
            ["close", "open", "high", "low", "ma5", "ma10", "ma20", "volume", "pct_chg"],
        )
        self.assertEqual(enhanced["today"]["fetched_at"], "2026-05-31T10:00:05+00:00")
        self.assertEqual(enhanced["today"]["provider_timestamp"], "2026-05-31T10:00:00+00:00")
        self.assertEqual(enhanced["today"]["fallback_from"], "efinance")
        self.assertNotIn("amount", enhanced["today"])
        self.assertNotIn("dataSource", enhanced["today"])

    @patch("src.core.pipeline.get_market_now")
    @patch("src.core.pipeline.get_market_for_stock", return_value="cn")
    def test_realtime_today_does_not_backfill_historical_amount_or_source(
        self, _mock_market, mock_now
    ) -> None:
        today = date.today()
        mock_now.return_value = datetime(
            today.year, today.month, today.day, 10, 0, tzinfo=timezone.utc
        )
        context = {
            "code": "600519",
            "date": (today - timedelta(days=1)).isoformat(),
            "today": {
                "close": 15.0,
                "amount": 999999,
                "date": (today - timedelta(days=1)).isoformat(),
                "dataSource": "AkshareFetcher",
                "code": "600519",
            },
            "yesterday": {"close": 14.5, "volume": 1000000},
        }
        quote = _make_realtime_quote(price=15.72, amount=None)
        trend = TrendAnalysisResult(
            code="600519",
            trend_status=TrendStatus.BULL,
            ma5=15.5,
            ma10=15.2,
            ma20=14.9,
        )

        enhanced = self.pipeline._enhance_context(
            context, quote, None, trend, "贵州茅台"
        )

        self.assertNotIn("amount", enhanced["today"])
        self.assertNotIn("dataSource", enhanced["today"])
        self.assertEqual(enhanced["today"]["date"], today.isoformat())
        self.assertEqual(enhanced["today"]["data_source"], "realtime:tencent")
        self.assertEqual(enhanced["today"]["code"], "600519")

    def test_enhance_context_injects_runtime_news_window_days(self) -> None:
        context = {"code": "600519", "today": {"close": 15.0}}
        enhanced = self.pipeline._enhance_context(
            context, None, None, None, "贵州茅台"
        )
        self.assertEqual(
            enhanced["news_window_days"],
            self.pipeline.search_service.news_window_days,
        )

    def test_today_not_overridden_when_trend_missing(self) -> None:
        context = {"code": "600519", "today": {"close": 15.0}}
        quote = _make_realtime_quote(price=15.72)
        enhanced = self.pipeline._enhance_context(
            context, quote, None, None, "贵州茅台"
        )
        self.assertEqual(enhanced["today"]["close"], 15.0)

    def test_today_not_overridden_when_realtime_missing(self) -> None:
        context = {"code": "600519", "today": {"close": 15.0}}
        trend = TrendAnalysisResult(code="600519", ma5=15.0, ma10=14.8, ma20=14.5)
        enhanced = self.pipeline._enhance_context(
            context, None, None, trend, "贵州茅台"
        )
        self.assertEqual(enhanced["today"]["close"], 15.0)

    def test_today_not_overridden_when_trend_ma_zero(self) -> None:
        """StockTrendAnalyzer 因数据不足提前返回 ma5=0.0 时，不应覆盖 today。"""
        context = {"code": "600519", "today": {"close": 15.0, "ma5": 14.8}}
        quote = _make_realtime_quote(price=15.72)
        trend = TrendAnalysisResult(code="600519")  # 默认 ma5=ma10=ma20=0.0
        enhanced = self.pipeline._enhance_context(
            context, quote, None, trend, "贵州茅台"
        )
        self.assertEqual(enhanced["today"]["close"], 15.0)
        self.assertEqual(enhanced["today"]["ma5"], 14.8)


if __name__ == "__main__":
    unittest.main()
