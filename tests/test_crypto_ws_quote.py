# -*- coding: utf-8 -*-
"""Tests for the OKX WebSocket quote cache."""

from __future__ import annotations

import json
import logging
import time
from types import SimpleNamespace

from data_provider.crypto_ws_quote import OKXTickerWebSocketQuoteFetcher, _resolve_websocket_connect


def test_websocket_quote_fetcher_returns_fresh_cached_quote() -> None:
    fetcher = OKXTickerWebSocketQuoteFetcher(
        stale_after_seconds=20,
        rest_fetcher=SimpleNamespace(get_realtime_quote=lambda _symbol: {"price": 1.0}),
    )

    fetcher._handle_message(
        "BTCUSDT",
        json.dumps(
            {
                "data": [
                    {
                        "ts": "1710000000000",
                        "last": "65000.5",
                        "vol24h": "123.45",
                        "volCcy24h": "8020000",
                        "open24h": "64230.4",
                        "high24h": "65100.0",
                        "low24h": "64000.0",
                    }
                ],
            }
        ),
    )

    quote = fetcher._fresh_cached_quote("BTCUSDT")

    assert quote is not None
    assert quote["price"] == 65000.5
    assert quote["source"] == "okx_ws"
    assert quote["provider_timestamp"] == "2024-03-09T16:00:00+00:00"


def test_websocket_quote_fetcher_ignores_stale_cached_quote() -> None:
    fetcher = OKXTickerWebSocketQuoteFetcher(
        stale_after_seconds=1,
        rest_fetcher=SimpleNamespace(get_realtime_quote=lambda _symbol: {"price": 1.0}),
    )
    with fetcher._lock:
        fetcher._latest["BTCUSDT"] = {
            "price": 65000.0,
            "_received_at": time.time() - 10,
        }

    assert fetcher._fresh_cached_quote("BTCUSDT") is None


def test_websocket_quote_fetcher_logs_first_and_periodic_ticks(caplog) -> None:
    fetcher = OKXTickerWebSocketQuoteFetcher(
        stale_after_seconds=20,
        rest_fetcher=SimpleNamespace(get_realtime_quote=lambda _symbol: {"price": 1.0}),
    )
    message = json.dumps({"data": [{"ts": "1710000000000", "last": "65000.5"}]})

    with caplog.at_level(logging.INFO, logger="data_provider.crypto_ws_quote"):
        fetcher._handle_message("BTCUSDT", message)

        assert "已收到首条有效行情" in caplog.text

        caplog.clear()
        fetcher._last_message_log_at["BTCUSDT"] = 0.0
        fetcher._handle_message("BTCUSDT", message)

    assert "WebSocket 行情运行中" in caplog.text


def test_websocket_quote_fetcher_throttles_rest_fallback_logs(caplog) -> None:
    fetcher = OKXTickerWebSocketQuoteFetcher(
        stale_after_seconds=20,
        rest_fetcher=SimpleNamespace(get_realtime_quote=lambda _symbol: {"price": 1.0}),
    )
    fetcher._websockets_unavailable = True

    with caplog.at_level(logging.INFO, logger="data_provider.crypto_ws_quote"):
        fetcher("BTC")
        fetcher("BTC")

    assert caplog.text.count("使用 REST 行情兜底") == 1


def test_websocket_connect_prefers_legacy_client() -> None:
    connect = _resolve_websocket_connect()

    assert "websockets.legacy.client" in connect.__module__
