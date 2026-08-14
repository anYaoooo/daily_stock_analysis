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


def _order_flow_klines(*, taker_buy_volume: str = "7", price_step: int = 5):
    rows = []
    for index in range(36):
        opened_at = 1709900000000 + index * 300000
        open_price = 65000 + index * price_step
        close_price = open_price + price_step
        rows.append(
            [
                opened_at,
                str(open_price),
                str(max(open_price, close_price) + 10),
                str(min(open_price, close_price) - 10),
                str(close_price),
                "10",
                opened_at + 299999,
                "0",
                100,
                taker_buy_volume,
            ]
        )
    return rows


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
            _response(
                [
                    {"fundingRate": "0.00010", "fundingTime": 1709900000000},
                    {"fundingRate": "0.00020", "fundingTime": 1709930000000},
                    {"fundingRate": "0.00040", "fundingTime": 1709960000000},
                ]
            ),
            _response(
                [
                    {"sumOpenInterest": "170000", "timestamp": 1709900000000},
                    {"sumOpenInterest": "180000", "timestamp": 1709990000000},
                ]
            ),
            _response(
                [
                    {"longShortRatio": "0.95", "timestamp": 1709900000000},
                    {"longShortRatio": "1.25", "timestamp": 1709990000000},
                ]
            ),
            _response(_order_flow_klines()),
            _response(
                {
                    "data": [
                        {
                            "fundingRate": "0.00055",
                            "markPx": "65010",
                            "indexPx": "64990",
                        }
                    ]
                }
            ),
            _response(
                {
                    "result": {
                        "list": [
                            {
                                "fundingRate": "0.00050",
                                "markPrice": "65005",
                                "indexPrice": "64995",
                            }
                        ]
                    }
                }
            ),
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
    assert context["funding"]["history_7d"]["trend"] == "rising"
    assert context["open_interest"]["history_24h"]["change_pct"] > 5
    assert context["basis"]["state"] == "flat"
    assert context["long_short_ratio"]["state"] == "long_heavy"
    assert context["order_flow"]["data_quality"] == "available"
    assert context["order_flow"]["state"] == "buy_dominant"
    assert context["order_flow"]["taker_buy_ratio_pct"] == 70.0
    assert context["order_flow"]["cvd_pct_of_volume"] == 40.0
    assert context["order_flow"]["divergence"] == "aligned_buying"
    assert context["cross_exchange"]["available_venues"] == 3
    assert context["cross_exchange"]["data_quality"] == "cross_checked"
    assert context["leverage_pressure"] == "long_crowding_risk"
    assert get.call_count == 8


def test_crypto_derivatives_fetcher_returns_unavailable_when_public_data_missing() -> None:
    fetcher = CryptoDerivativesFetcher()
    with patch(
        "data_provider.crypto_derivatives_fetcher.requests.get",
        side_effect=[
            _response({}),
            _response({}),
            _response([]),
            _response([]),
            _response([]),
            _response([]),
        ],
    ):
        context = fetcher.get_btc_derivatives_context("BTCUSDT")

    assert context is not None
    assert context["data_quality"] == "unavailable"
    assert context["warnings"] == ["funding_rate_open_interest_and_order_flow_missing"]


def test_crypto_derivatives_fetcher_detects_bearish_price_cvd_divergence() -> None:
    summary = CryptoDerivativesFetcher._order_flow_summary(
        _order_flow_klines(taker_buy_volume="2", price_step=20),
        interval_minutes=5,
    )

    assert summary["state"] == "sell_dominant"
    assert summary["price_change_pct"] > 0.25
    assert summary["cvd_pct_of_volume"] == -60.0
    assert summary["divergence"] == "bearish_price_cvd_divergence"


def test_crypto_derivatives_fetcher_ignores_non_btc_symbols() -> None:
    fetcher = CryptoDerivativesFetcher()

    assert fetcher.get_btc_derivatives_context("ETHUSDT") is None
