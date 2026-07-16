# -*- coding: utf-8 -*-
"""Tests for cryptocurrency market-data routing."""

from __future__ import annotations

from unittest.mock import Mock, patch
from datetime import datetime, timezone

import pandas as pd
import pytest

from data_provider.base import BaseFetcher, DataFetcherManager, DataFetchError
from data_provider.crypto_fetcher import CryptoFetcher, normalize_crypto_symbol
from data_provider.realtime_types import RealtimeSource
from src.schemas.crypto_instrument import resolve_crypto_instrument


def test_normalize_crypto_symbol_accepts_common_btc_aliases() -> None:
    assert normalize_crypto_symbol("BTC") == "BTCUSDT"
    assert normalize_crypto_symbol("BTCUSDT") == "BTCUSDT"
    assert normalize_crypto_symbol("BTC-USD") == "BTCUSDT"
    assert normalize_crypto_symbol("BTC/USD") == "BTCUSDT"
    assert normalize_crypto_symbol("AAPL") is None


def test_instrument_contract_keeps_spot_and_perpetual_distinct() -> None:
    spot = resolve_crypto_instrument("BTC/USDT", default_type="perpetual", venue="okx")
    perpetual = resolve_crypto_instrument("BTC-USDT-PERP", default_type="spot", venue="okx")

    assert spot is not None and spot.instrument_type == "spot"
    assert spot.market_symbol == "BTC/USDT"
    assert perpetual is not None and perpetual.instrument_type == "perpetual"
    assert perpetual.market_symbol == "BTC/USDT:USDT"
    assert perpetual.liquidation_price_type == "mark"


def test_crypto_fetcher_parses_okx_daily_klines() -> None:
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
        limit=300,
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


def test_crypto_fetcher_paginates_okx_klines_until_requested_range_is_covered() -> None:
    first_page = [
        [1717200000000 + index * 3600000, 67000.0, 68000.0, 66000.0, 67500.0, 10.0]
        for index in range(300)
    ]
    second_page = [
        [first_page[-1][0] + 3600000, 67500.0, 68500.0, 67000.0, 68000.0, 11.0]
    ]
    exchange = Mock()
    exchange.fetch_ohlcv.side_effect = [first_page, second_page]

    fetcher = CryptoFetcher()
    with patch.object(fetcher, "_create_exchange", return_value=exchange):
        raw_df = fetcher._fetch_kline_rows(
            "BTC",
            start_date="2024-06-01",
            end_date="2024-06-30",
            period="hourly",
        )

    assert exchange.fetch_ohlcv.call_count == 2
    assert exchange.fetch_ohlcv.call_args_list[1].kwargs["since"] == first_page[-1][0] + 1
    assert len(raw_df) == 301


def test_crypto_fetcher_parses_okx_realtime_quote() -> None:
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
    assert quote.source == RealtimeSource.OKX
    assert quote.price == 68500.0
    assert quote.change_pct == 2.086
    assert quote.amount == 16067960.0


def test_crypto_fetcher_falls_back_to_okx_rest_when_ccxt_ticker_fails() -> None:
    rest_payload = {
        "code": "0",
        "msg": "",
        "data": [
            {
                "instId": "BTC-USDT",
                "last": "68500.0",
                "open24h": "67100.0",
                "high24h": "69000.0",
                "low24h": "67000.0",
                "vol24h": "234.56",
                "volCcy24h": "16067960.0",
                "ts": "1717372799999",
            }
        ],
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
    assert quote.source == RealtimeSource.OKX
    assert quote.price == 68500.0
    assert quote.change_pct == ((68500.0 - 67100.0) / 67100.0 * 100)
    assert quote.amount == 16067960.0


def test_crypto_fetcher_falls_back_to_binance_when_okx_is_unavailable() -> None:
    exchange = Mock()
    exchange.fetch_ticker.side_effect = Exception("OKX unavailable")
    binance_payload = {
        "last": "68600",
        "percentage": "1.5",
        "baseVolume": "10",
        "quoteVolume": "686000",
    }
    fetcher = CryptoFetcher()

    with patch.object(fetcher, "_create_exchange", return_value=exchange), patch.object(
        fetcher, "_fetch_okx_rest_ticker", side_effect=Exception("OKX REST unavailable")
    ), patch.object(
        CryptoFetcher, "_fetch_binance_rest_ticker", return_value=binance_payload
    ) as binance_fetch, patch.object(
        CryptoFetcher, "_fetch_bybit_rest_ticker"
    ) as bybit_fetch:
        quote = fetcher.get_realtime_quote("BTC")

    assert quote is not None
    assert quote.source == RealtimeSource.BINANCE
    assert quote.price == 68600.0
    binance_fetch.assert_called_once_with("BTCUSDT")
    bybit_fetch.assert_not_called()


def test_crypto_fetcher_falls_back_to_bybit_after_okx_and_binance_fail() -> None:
    exchange = Mock()
    exchange.fetch_ticker.side_effect = Exception("OKX unavailable")
    bybit_payload = {"last": "68700", "percentage": "2.0"}
    fetcher = CryptoFetcher()

    with patch.object(fetcher, "_create_exchange", return_value=exchange), patch.object(
        fetcher, "_fetch_okx_rest_ticker", side_effect=Exception("OKX REST unavailable")
    ), patch.object(
        CryptoFetcher, "_fetch_binance_rest_ticker", side_effect=Exception("Binance unavailable")
    ), patch.object(CryptoFetcher, "_fetch_bybit_rest_ticker", return_value=bybit_payload):
        quote = fetcher.get_realtime_quote("BTC")

    assert quote is not None
    assert quote.source == RealtimeSource.BYBIT
    assert quote.price == 68700.0


def test_crypto_fetcher_cools_down_okx_after_full_provider_failure() -> None:
    exchange = Mock()
    exchange.fetch_ticker.side_effect = Exception("OKX unavailable")
    fetcher = CryptoFetcher(clock=lambda: 1000.0)
    binance_payload = {"last": "68600"}

    with patch.object(fetcher, "_create_exchange", return_value=exchange), patch.object(
        fetcher, "_fetch_okx_rest_ticker", side_effect=Exception("OKX REST unavailable")
    ) as okx_rest, patch.object(
        CryptoFetcher, "_fetch_binance_rest_ticker", return_value=binance_payload
    ) as binance_fetch:
        first = fetcher.get_realtime_quote("BTC")
        second = fetcher.get_realtime_quote("BTC")

    assert first is not None and second is not None
    assert exchange.fetch_ticker.call_count == 1
    assert okx_rest.call_count == 1
    assert binance_fetch.call_count == 2


def test_crypto_fetcher_falls_back_to_public_kline_provider() -> None:
    fallback_rows = [[1717200000000, 67400.0, 68000.0, 66000.0, 67100.0, 123.45]]
    fetcher = CryptoFetcher()

    with patch.object(fetcher, "_create_exchange", side_effect=Exception("OKX unavailable")), patch.object(
        fetcher, "_fetch_fallback_kline_rows", return_value=fallback_rows
    ) as fallback:
        df = fetcher.get_daily_data("BTC", start_date="2024-06-01", end_date="2024-06-01")

    assert len(df) == 1
    fallback.assert_called_once()


def test_crypto_fetcher_builds_aligned_perpetual_trade_mark_and_funding_data() -> None:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - 2 * 60 * 60 * 1000
    trade_rows = [
        [start_ms, 100.0, 102.0, 99.0, 101.0, 10.0],
        [start_ms + 3600000, 101.0, 104.0, 100.0, 103.0, 11.0],
    ]
    mark_rows = [
        [start_ms, 99.5, 101.5, 98.5, 100.5, 0.0],
        [start_ms + 3600000, 100.5, 103.5, 99.5, 102.5, 0.0],
    ]
    exchange = Mock()
    exchange.fetch_ohlcv.side_effect = [trade_rows, mark_rows]
    exchange.fetch_funding_rate_history.return_value = [
        {"timestamp": start_ms, "fundingRate": 0.0001}
    ]
    fetcher = CryptoFetcher()

    with patch.object(fetcher, "_create_public_exchange", return_value=exchange):
        frame = fetcher.get_perpetual_kline_data(
            "BTC-USDT-PERP",
            period="hourly",
            days=1,
            venue="okx",
        )

    assert frame["execution_open"].tolist() == [100.0, 101.0]
    assert frame["mark_open"].tolist() == [99.5, 100.5]
    assert frame.iloc[0]["funding_rates"] == (0.0001,)
    assert frame.attrs["instrument_type"] == "perpetual"
    assert frame.attrs["market_symbol"] == "BTC/USDT:USDT"
    assert frame.attrs["funding_complete"] is True


def test_crypto_fetcher_rejects_unsynchronized_perpetual_mark_candles() -> None:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    trade_rows = [[now_ms - 3600000, 100.0, 102.0, 99.0, 101.0, 10.0]]
    mark_rows = [[now_ms - 3500000, 99.5, 101.5, 98.5, 100.5, 0.0]]
    exchange = Mock()
    exchange.fetch_ohlcv.side_effect = [trade_rows, mark_rows]
    fetcher = CryptoFetcher()

    with patch.object(fetcher, "_create_public_exchange", return_value=exchange), pytest.raises(
        DataFetchError,
        match="unsynchronized",
    ):
        fetcher.get_perpetual_kline_data("BTC-USDT-PERP", period="hourly", days=1)


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
