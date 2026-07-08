# -*- coding: utf-8 -*-
"""Tests for public BTC derivatives context fetching."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from data_provider.crypto_derivatives_fetcher import CryptoDerivativesFetcher


def _response(payload):
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def test_crypto_derivatives_fetcher_builds_funding_and_open_interest_context() -> None:
    fetcher = CryptoDerivativesFetcher()
    with patch(
        "data_provider.crypto_derivatives_fetcher.requests.get",
        side_effect=[
            _response(
                {
                    "lastFundingRate": "0.00063",
                    "markPrice": "65000",
                    "indexPrice": "64980",
                    "nextFundingTime": 1710000000000,
                    "time": 1709990000000,
                }
            ),
            _response({"openInterest": "180000", "time": 1709990100000}),
        ],
    ) as get:
        context = fetcher.get_btc_derivatives_context("BTC")

    assert context is not None
    assert context["provider"] == "binance_futures"
    assert context["symbol"] == "BTCUSDT"
    assert context["data_quality"] == "available"
    assert context["funding"]["rate"] == 0.00063
    assert context["funding"]["rate_pct"] == 0.063
    assert context["funding"]["state"] == "positive_crowded"
    assert context["open_interest"]["value"] == 180000
    assert context["open_interest"]["state"] == "high_notional"
    assert context["open_interest"]["notional_usdt"] == 11700000000
    assert context["leverage_pressure"] == "long_crowding_risk"
    assert get.call_count == 2


def test_crypto_derivatives_fetcher_returns_unavailable_when_public_data_missing() -> None:
    fetcher = CryptoDerivativesFetcher()
    with patch(
        "data_provider.crypto_derivatives_fetcher.requests.get",
        side_effect=[_response({}), _response({})],
    ):
        context = fetcher.get_btc_derivatives_context("BTCUSDT")

    assert context is not None
    assert context["data_quality"] == "unavailable"
    assert context["warnings"] == ["funding_rate_and_open_interest_missing"]


def test_crypto_derivatives_fetcher_ignores_non_btc_symbols() -> None:
    fetcher = CryptoDerivativesFetcher()

    assert fetcher.get_btc_derivatives_context("ETHUSDT") is None
