from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pandas as pd

from src.services.crypto_market_data_service import CryptoMarketDataService
from src.services.stock_service import StockService
from src.storage import DatabaseManager


def _frame(rows, **attrs):
    frame = pd.DataFrame(rows)
    frame.attrs.update(attrs)
    return frame


def test_daily_cache_refreshes_once_then_serves_local_rows() -> None:
    DatabaseManager.reset_instance()
    db = DatabaseManager(db_url="sqlite:///:memory:")
    fetcher = Mock()
    fetcher.get_kline_data.return_value = _frame(
        {
            "date": ["2026-08-05", "2026-08-06", "2026-08-07"],
            "open": [100.0, 101.0, 102.0],
            "high": [101.0, 102.0, 103.0],
            "low": [99.0, 100.0, 101.0],
            "close": [100.5, 101.5, 102.5],
            "volume": [10.0, 11.0, 12.0],
        },
        source="okx",
    )
    service = CryptoMarketDataService(
        db_manager=db,
        fetcher=fetcher,
        now_provider=lambda: datetime(2026, 8, 7, 12, 30, tzinfo=timezone.utc),
    )

    first = service.get_bars("BTC", period="daily", days=2)
    second = service.get_bars("BTC", period="daily", days=2)

    assert list(first["close"]) == [100.5, 101.5]
    assert list(second["close"]) == [100.5, 101.5]
    assert fetcher.get_kline_data.call_count == 1
    assert second.attrs["source"] == "local_cache"
    assert second.attrs["cached_source"] == "okx"
    DatabaseManager.reset_instance()


def test_hourly_days_is_calendar_day_lookback() -> None:
    start, end = CryptoMarketDataService._requested_window(
        datetime(2026, 8, 10, 0, 10, tzinfo=timezone.utc),
        period="hourly",
        days=7,
    )

    assert end == datetime(2026, 8, 9, 23, tzinfo=timezone.utc)
    assert start == datetime(2026, 8, 3, 0, tzinfo=timezone.utc)


def test_cache_excludes_current_partial_bar() -> None:
    DatabaseManager.reset_instance()
    db = DatabaseManager(db_url="sqlite:///:memory:")
    fetcher = Mock()
    fetcher.get_kline_data.return_value = _frame(
        {
            "date": ["2026-08-06 11:00", "2026-08-06 12:00"],
            "open": [100.0, 101.0],
            "high": [101.0, 102.0],
            "low": [99.0, 100.0],
            "close": [100.5, 101.5],
            "volume": [10.0, 11.0],
        },
        source="okx",
    )
    service = CryptoMarketDataService(
        db_manager=db,
        fetcher=fetcher,
        now_provider=lambda: datetime(2026, 8, 6, 12, 30, tzinfo=timezone.utc),
    )

    bars = service.get_bars("BTC", period="hourly", days=1)

    assert list(bars["date"]) == ["2026-08-06 11:00"]
    assert len(db.get_crypto_ohlcv_bars(
        code="BTCUSDT", venue="okx", instrument_type="spot", price_type="trade", period="hourly",
        start_at=datetime(2026, 8, 6, 11), end_at=datetime(2026, 8, 6, 12),
    )) == 1
    DatabaseManager.reset_instance()


def test_perpetual_cache_preserves_mark_and_funding_fields() -> None:
    DatabaseManager.reset_instance()
    db = DatabaseManager(db_url="sqlite:///:memory:")
    fetcher = Mock()
    fetcher.get_perpetual_kline_data.return_value = _frame(
        {
            "date": ["2026-08-06 11:00"],
            "open": [100.0], "high": [102.0], "low": [99.0], "close": [101.0], "volume": [10.0],
            "execution_open": [100.0], "execution_high": [102.0],
            "execution_low": [99.0], "execution_close": [101.0],
            "mark_open": [100.1], "mark_high": [101.9], "mark_low": [99.2], "mark_close": [100.8],
            "funding_rates": [(0.0001,)], "funding_complete": [True],
        },
        source="okx_ccxt", venue="okx", instrument_type="perpetual", price_type="trade_and_mark",
    )
    service = CryptoMarketDataService(
        db_manager=db,
        fetcher=fetcher,
        now_provider=lambda: datetime(2026, 8, 6, 12, 30, tzinfo=timezone.utc),
    )

    bars = service.get_bars(
        "BTC", period="hourly", days=1,
        instrument={"type": "perpetual", "venue": "okx", "margin_mode": "isolated"},
    )

    assert float(bars.iloc[0]["mark_close"]) == 100.8
    assert list(bars.iloc[0]["funding_rates"]) == [0.0001]
    assert bars.attrs["instrument_type"] == "perpetual"
    assert fetcher.get_perpetual_kline_data.call_count == 1
    DatabaseManager.reset_instance()


def test_history_api_keeps_non_cached_crypto_periods_on_existing_fetcher() -> None:
    fetcher = Mock()
    fetcher.name = "CryptoFetcher"
    fetcher.get_kline_data.return_value = _frame(
        {
            "date": ["2026-08-03 00:00"],
            "open": [100.0], "high": [102.0], "low": [99.0], "close": [101.0], "volume": [10.0],
        }
    )
    manager = Mock()
    manager.get_stock_name.return_value = "Bitcoin"

    with patch("data_provider.crypto_fetcher.CryptoFetcher", return_value=fetcher), patch(
        "data_provider.base.DataFetcherManager", return_value=manager
    ):
        response = StockService().get_history_data("BTC", period="four_hour", days=1)

    assert response["data"][0]["close"] == 101.0
    fetcher.get_kline_data.assert_called_once_with("BTC", period="four_hour", days=1)
