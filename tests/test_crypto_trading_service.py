# -*- coding: utf-8 -*-
"""Tests for CCXT private trading service safety behavior."""

from __future__ import annotations

from unittest.mock import Mock

from src.services.crypto_trading_service import CryptoTradingConfigError
from src.services.crypto_trading_service import CryptoTradingService


class DummyConfig:
    crypto_trading_exchange = "okx"
    crypto_trading_enabled = False
    crypto_trading_dry_run = True
    crypto_trading_default_symbol = "BTC/USDT:USDT"
    crypto_trading_timeout_ms = 10000
    okx_api_key = "key"
    okx_secret = "secret"
    okx_password = "passphrase"
    okx_sandbox = False
    okx_default_type = "swap"
    okx_td_mode = "cross"
    bybit_api_key = ""
    bybit_secret = ""
    bybit_demo_trading = False
    bybit_default_type = "swap"
    bybit_default_settle = "USDT"
    bybit_margin_mode = "isolated"
    bybit_default_leverage = 2


class RealTradingBlockedConfig(DummyConfig):
    crypto_trading_dry_run = False


class BybitDemoConfig(DummyConfig):
    crypto_trading_exchange = "bybit"
    bybit_api_key = "demo-key"
    bybit_secret = "demo-secret"
    bybit_demo_trading = True


class BybitSpotDemoConfig(BybitDemoConfig):
    crypto_trading_default_symbol = ""
    bybit_default_type = "spot"


class BybitDemoDisabledConfig(BybitDemoConfig):
    bybit_demo_trading = False


def test_create_order_dry_run_does_not_call_exchange() -> None:
    exchange = Mock()
    service = CryptoTradingService(exchange=exchange)
    service.config = DummyConfig()

    result = service.create_order(
        symbol=None,
        order_type="limit",
        side="buy",
        amount=0.01,
        price=60000,
        pos_side="long",
    )

    assert result["dry_run"] is True
    assert result["order"]["symbol"] == "BTC/USDT:USDT"
    assert result["order"]["params"]["tdMode"] == "cross"
    assert result["order"]["params"]["posSide"] == "long"
    exchange.create_order.assert_not_called()


def test_cancel_order_dry_run_does_not_call_exchange() -> None:
    exchange = Mock()
    service = CryptoTradingService(exchange=exchange)
    service.config = DummyConfig()

    result = service.cancel_order(order_id="abc", symbol="BTC")

    assert result["dry_run"] is True
    assert result["cancel"]["symbol"] == "BTC/USDT:USDT"
    exchange.cancel_order.assert_not_called()


def test_real_order_requires_trading_enabled() -> None:
    exchange = Mock()
    service = CryptoTradingService(exchange=exchange)
    service.config = RealTradingBlockedConfig()

    try:
        service.create_order(
            symbol="BTC",
            order_type="market",
            side="buy",
            amount=0.01,
            pos_side="long",
        )
    except CryptoTradingConfigError:
        pass
    else:
        raise AssertionError("real order should require CRYPTO_TRADING_ENABLED=true")

    exchange.create_order.assert_not_called()


def test_bybit_futures_demo_order_uses_position_idx_and_order_link_id() -> None:
    exchange = Mock()
    service = CryptoTradingService(exchange=exchange)
    service.config = BybitDemoConfig()

    result = service.create_order(
        symbol="BTC",
        order_type="limit",
        side="buy",
        amount=0.001,
        price=50000,
        td_mode="isolated",
        pos_side="long",
        client_order_id="demo-1",
    )

    assert result["dry_run"] is True
    assert result["order"]["exchange"] == "bybit"
    assert result["order"]["symbol"] == "BTC/USDT:USDT"
    assert result["order"]["params"]["position_idx"] == 0
    assert result["order"]["params"]["orderLinkId"] == "demo-1"
    assert "tdMode" not in result["order"]["params"]
    exchange.create_order.assert_not_called()


def test_bybit_spot_default_symbol_uses_spot_symbol() -> None:
    exchange = Mock()
    service = CryptoTradingService(exchange=exchange)
    service.config = BybitSpotDemoConfig()

    result = service.create_order(
        symbol=None,
        order_type="limit",
        side="buy",
        amount=0.001,
        price=50000,
    )

    assert result["dry_run"] is True
    assert result["order"]["symbol"] == "BTC/USDT"
    assert result["order"]["params"] == {}


def test_bybit_requires_demo_trading_flag() -> None:
    service = CryptoTradingService()
    service.config = BybitDemoDisabledConfig()

    try:
        service._create_bybit_exchange()
    except CryptoTradingConfigError as exc:
        assert "BYBIT_DEMO_TRADING=true" in str(exc)
    else:
        raise AssertionError("Bybit should require BYBIT_DEMO_TRADING=true")
