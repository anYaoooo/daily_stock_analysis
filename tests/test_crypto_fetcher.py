# -*- coding: utf-8 -*-
"""Tests for cryptocurrency market-data routing."""

from __future__ import annotations

from unittest.mock import Mock, patch

import pandas as pd

from data_provider.base import BaseFetcher, DataFetcherManager
from data_provider.crypto_fetcher import CryptoFetcher, normalize_crypto_symbol
from data_provider.realtime_types import RealtimeSource


def test_normalize_crypto_symbol_accepts_common_btc_aliases() -> None:
    assert normalize_crypto_symbol("BTC") == "BTCUSDT"
    assert normalize_crypto_symbol("BTCUSDT") == "BTCUSDT"
    assert normalize_crypto_symbol("BTC-USD") == "BTCUSDT"
    assert normalize_crypto_symbol("BTC/USD") == "BTCUSDT"
    assert normalize_crypto_symbol("AAPL") is None


def test_crypto_fetcher_parses_binance_daily_klines() -> None:
    payload = [
        [1717200000000, 67400.0, 68000.0, 66000.0, 67100.0, 123.45],
        [1717286400000, 67100.0, 69000.0, 67000.0, 68500.0, 234.56],
    ]
    exchange = Mock()
    exchange.fetch_ohlcv.return_value = payload

    fetcher = CryptoFetcher()
    with patch.object(fetcher, "_create_exchange", return_value=exchange):
        df = fetcher.get_daily_data("BTC-USD", start_date="2024-06-01", end_date="2024-06-02")

    exchange.fetch_ohlcv.assert_called_once_with(
        "BTC/USDT",
        timeframe="1d",
        since=1717200000000,
        limit=1000,
    )
    assert list(df.columns) == [
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "pct_chg",
        "ma5",
        "ma10",
        "ma20",
        "volume_ratio",
    ]
    assert len(df) == 2
    assert float(df.iloc[1]["close"]) == 68500.0
    assert float(df.iloc[1]["amount"]) == 68500.0 * 234.56


def test_crypto_fetcher_parses_binance_realtime_quote() -> None:
    payload = {
        "last": 68500.0,
        "change": 1400.0,
        "percentage": 2.086,
        "baseVolume": 234.56,
        "quoteVolume": 16067960.0,
        "open": 67100.0,
        "high": 69000.0,
        "low": 67000.0,
        "previousClose": 67100.0,
        "timestamp": 1717372799999,
    }
    exchange = Mock()
    exchange.fetch_ticker.return_value = payload

    fetcher = CryptoFetcher()
    with patch.object(fetcher, "_create_exchange", return_value=exchange):
        quote = fetcher.get_realtime_quote("BTC")

    exchange.fetch_ticker.assert_called_once_with("BTC/USDT")
    assert quote is not None
    assert quote.code == "BTCUSDT"
    assert quote.name == "Bitcoin"
    assert quote.source == RealtimeSource.BINANCE
    assert quote.price == 68500.0
    assert quote.change_pct == 2.086
    assert quote.amount == 16067960.0


def test_crypto_fetcher_falls_back_to_binance_rest_when_ccxt_ticker_fails() -> None:
    rest_payload = {
        "lastPrice": "68500.0",
        "priceChange": "1400.0",
        "priceChangePercent": "2.086",
        "volume": "234.56",
        "quoteVolume": "16067960.0",
        "openPrice": "67100.0",
        "highPrice": "69000.0",
        "lowPrice": "67000.0",
        "prevClosePrice": "67100.0",
        "closeTime": 1717372799999,
    }
    exchange = Mock()
    exchange.fetch_ticker.side_effect = Exception("404 Not Found")

    fetcher = CryptoFetcher()
    with patch.object(fetcher, "_create_exchange", return_value=exchange), patch(
        "data_provider.crypto_fetcher.requests.get"
    ) as get_mock:
        response = Mock()
        response.json.return_value = rest_payload
        response.raise_for_status.return_value = None
        get_mock.return_value = response

        quote = fetcher.get_realtime_quote("BTC")

    exchange.fetch_ticker.assert_called_once_with("BTC/USDT")
    get_mock.assert_called_once()
    assert quote is not None
    assert quote.code == "BTCUSDT"
    assert quote.source == RealtimeSource.BINANCE
    assert quote.price == 68500.0
    assert quote.change_pct == 2.086
    assert quote.amount == 16067960.0


def test_manager_routes_crypto_daily_data_to_crypto_fetcher_only() -> None:
    crypto_fetcher = CryptoFetcher()
    manager = DataFetcherManager(fetchers=[crypto_fetcher])
    fake_daily = pd.DataFrame(
        {
            "date": ["2024-06-01"],
            "open": [67000.0],
            "high": [69000.0],
            "low": [66000.0],
            "close": [68500.0],
            "volume": [1.0],
            "amount": [68500.0],
        }
    )

    with patch.object(crypto_fetcher, "get_daily_data", return_value=fake_daily) as get_daily:
        result, source = manager.get_daily_data("BTC/USD", days=5)

    assert result is fake_daily
    assert source == "CryptoFetcher"
    get_daily.assert_called_once_with(
        stock_code="BTC",
        start_date=None,
        end_date=None,
        days=5,
    )


def test_manager_skips_chip_distribution_for_crypto_symbols() -> None:
    class ChipFetcher(BaseFetcher):
        name = "ChipFetcher"
        priority = 0

        def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
            return pd.DataFrame()

        def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
            return df

        def get_chip_distribution(self, stock_code: str):  # pragma: no cover - must not be called
            raise AssertionError("crypto symbols must not call chip distribution providers")

    manager = DataFetcherManager(fetchers=[ChipFetcher()])

    assert manager.get_chip_distribution("BTC") is None
    assert manager.get_chip_distribution("BTCUSDT") is None
    assert manager.get_chip_distribution("BTC-USD") is None
    assert manager.get_chip_distribution("BTC/USD") is None
