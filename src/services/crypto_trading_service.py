# -*- coding: utf-8 -*-
"""CCXT private trading service for configured crypto exchanges."""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.auth import is_auth_enabled
from src.config import get_config
from src.schemas.crypto_instrument import (
    CryptoInstrument,
    instrument_type_from_market_symbol,
    resolve_crypto_instrument,
)

logger = logging.getLogger(__name__)


class CryptoTradingConfigError(ValueError):
    """Raised when private trading configuration is incomplete or disabled."""


class CryptoTradingService:
    """Small CCXT private API wrapper for manual crypto trading.

    The service never auto-trades. Mutating calls are gated by both
    CRYPTO_TRADING_ENABLED and CRYPTO_TRADING_DRY_RUN.
    """

    def __init__(self, exchange: Optional[Any] = None):
        self.config = get_config()
        self._exchange = exchange

    @property
    def exchange(self) -> Any:
        if self._exchange is None:
            self._exchange = self._create_exchange()
        return self._exchange

    @property
    def exchange_id(self) -> str:
        return self._get_exchange_id()

    def fetch_balance(self, *, currency: Optional[str] = None) -> dict[str, Any]:
        payload = self.exchange.fetch_balance()
        if currency:
            key = currency.upper()
            return {
                "exchange": self.exchange_id,
                "currency": key,
                "balance": payload.get(key),
                "free": (payload.get("free") or {}).get(key),
                "used": (payload.get("used") or {}).get(key),
                "total": (payload.get("total") or {}).get(key),
                "raw": payload.get("info"),
            }
        return {"exchange": self.exchange_id, "balance": payload}

    def fetch_positions(self, *, symbol: Optional[str] = None) -> list[dict[str, Any]]:
        market_symbol = self._resolve_symbol(symbol)
        symbols = [market_symbol] if market_symbol else None
        positions = self.exchange.fetch_positions(symbols)
        return [self._safe_dict(item) for item in positions or []]

    def fetch_open_orders(self, *, symbol: Optional[str] = None) -> list[dict[str, Any]]:
        orders = self.exchange.fetch_open_orders(self._resolve_symbol(symbol))
        return [self._safe_dict(item) for item in orders or []]

    def fetch_order(self, *, order_id: str, symbol: Optional[str] = None) -> dict[str, Any]:
        return self._safe_dict(self.exchange.fetch_order(order_id, self._resolve_symbol(symbol)))

    def create_order(
        self,
        *,
        symbol: Optional[str],
        order_type: str,
        side: str,
        amount: float,
        price: Optional[float] = None,
        td_mode: Optional[str] = None,
        pos_side: Optional[str] = None,
        reduce_only: bool = False,
        client_order_id: Optional[str] = None,
        extra_params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        instrument = self._resolve_instrument(symbol, required=True)
        market_symbol = instrument.market_symbol
        normalized_type = self._normalize_order_type(order_type)
        normalized_side = self._normalize_side(side)
        normalized_amount = self._positive_float(amount, "amount")
        normalized_price = self._optional_positive_float(price, "price")

        params = self._build_order_params(
            instrument_type=instrument.instrument_type,
            td_mode=td_mode,
            pos_side=pos_side,
            reduce_only=reduce_only,
            client_order_id=client_order_id,
            extra_params=extra_params,
        )

        if normalized_type == "market":
            normalized_price = None
        elif normalized_price is None:
            raise ValueError("limit orders require price")

        preview = {
            "exchange": self.exchange_id,
            "symbol": market_symbol,
            "instrument": instrument.to_contract(),
            "type": normalized_type,
            "side": normalized_side,
            "amount": normalized_amount,
            "price": normalized_price,
            "params": params,
        }
        if self._is_dry_run():
            return {"dry_run": True, "order": preview}
        self._ensure_trading_enabled()

        result = self.exchange.create_order(
            market_symbol,
            normalized_type,
            normalized_side,
            normalized_amount,
            normalized_price,
            params,
        )
        return {"dry_run": False, "order": self._safe_dict(result)}

    def cancel_order(
        self,
        *,
        order_id: str,
        symbol: Optional[str] = None,
        extra_params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if not order_id:
            raise ValueError("order_id is required")
        market_symbol = self._resolve_symbol(symbol, required=True)
        params = dict(extra_params or {})
        preview = {"exchange": self.exchange_id, "order_id": order_id, "symbol": market_symbol, "params": params}
        if self._is_dry_run():
            return {"dry_run": True, "cancel": preview}
        self._ensure_trading_enabled()
        result = self.exchange.cancel_order(order_id, market_symbol, params)
        return {"dry_run": False, "cancel": self._safe_dict(result)}

    def set_leverage(
        self,
        *,
        leverage: int,
        symbol: Optional[str] = None,
        margin_mode: Optional[str] = None,
        pos_side: Optional[str] = None,
    ) -> dict[str, Any]:
        normalized_leverage = int(leverage)
        if normalized_leverage <= 0:
            raise ValueError("leverage must be positive")
        market_symbol = self._resolve_symbol(symbol, required=True)
        params = {}
        if margin_mode:
            self._apply_margin_mode_param(params, margin_mode)
        if pos_side:
            self._apply_pos_side_param(params, pos_side)

        if self._is_dry_run():
            return {
                "dry_run": True,
                "leverage": {
                    "symbol": market_symbol,
                    "leverage": normalized_leverage,
                    "params": params,
                    "position_mode": "oneway" if self._is_bybit_swap() else None,
                },
            }
        self._ensure_trading_enabled()
        if self._is_bybit_swap():
            self._prepare_bybit_futures_account()
            if margin_mode:
                result = self.exchange.set_margin_mode(
                    self._normalize_margin_mode(margin_mode),
                    market_symbol,
                    {"leverage": normalized_leverage},
                )
                result = self.exchange.set_leverage(normalized_leverage, market_symbol)
            else:
                result = self.exchange.set_leverage(normalized_leverage, market_symbol)
        else:
            result = self.exchange.set_leverage(normalized_leverage, market_symbol, params)
        return {"dry_run": False, "leverage": self._safe_dict(result)}

    def set_margin_mode(self, *, margin_mode: str, symbol: Optional[str] = None) -> dict[str, Any]:
        normalized = self._normalize_margin_mode(margin_mode)
        market_symbol = self._resolve_symbol(symbol, required=True)
        if self._is_dry_run():
            return {
                "dry_run": True,
                "margin_mode": {
                    "symbol": market_symbol,
                    "margin_mode": normalized,
                },
            }
        self._ensure_trading_enabled()
        params = self._build_set_margin_mode_params(normalized)
        if self._is_bybit_swap():
            self._prepare_bybit_futures_account()
        result = self.exchange.set_margin_mode(normalized, market_symbol, params)
        return {"dry_run": False, "margin_mode": self._safe_dict(result)}

    def _create_exchange(self) -> Any:
        exchange_id = self._get_exchange_id()
        if exchange_id == "okx":
            return self._create_okx_exchange()
        if exchange_id == "bybit":
            return self._create_bybit_exchange()
        raise CryptoTradingConfigError("CRYPTO_TRADING_EXCHANGE 仅支持 okx 或 bybit")

    def _create_okx_exchange(self) -> Any:
        try:
            import ccxt
        except ImportError as exc:
            raise CryptoTradingConfigError("ccxt 未安装，请运行 pip install ccxt") from exc

        api_key = (getattr(self.config, "okx_api_key", "") or "").strip()
        secret = (getattr(self.config, "okx_secret", "") or "").strip()
        password = (getattr(self.config, "okx_password", "") or "").strip()
        if not api_key or not secret or not password:
            raise CryptoTradingConfigError("OKX 私有 API 配置不完整：OKX_API_KEY / OKX_SECRET / OKX_PASSWORD 必须同时配置")

        exchange = ccxt.okx(
            {
                "apiKey": api_key,
                "secret": secret,
                "password": password,
                "enableRateLimit": True,
                "timeout": int(getattr(self.config, "crypto_trading_timeout_ms", 10000)),
                "options": {
                    "defaultType": getattr(self.config, "okx_default_type", "swap") or "swap",
                },
            }
        )
        if getattr(self.config, "okx_sandbox", False):
            exchange.set_sandbox_mode(True)
        return exchange

    def _create_bybit_exchange(self) -> Any:
        api_key = (getattr(self.config, "bybit_api_key", "") or "").strip()
        secret = (getattr(self.config, "bybit_secret", "") or "").strip()
        if not api_key or not secret:
            raise CryptoTradingConfigError("Bybit Demo Trading 配置不完整：BYBIT_API_KEY / BYBIT_SECRET 必须同时配置")
        if not getattr(self.config, "bybit_demo_trading", False):
            raise CryptoTradingConfigError("Bybit 接入仅支持交易所 Demo Trading，请设置 BYBIT_DEMO_TRADING=true")

        default_type = (getattr(self.config, "bybit_default_type", "swap") or "swap").strip().lower()
        if default_type not in {"spot", "swap"}:
            raise CryptoTradingConfigError("BYBIT_DEFAULT_TYPE 仅支持 spot 或 swap")

        options = {"defaultType": default_type}
        if default_type == "swap":
            settle = (getattr(self.config, "bybit_default_settle", "USDT") or "USDT").strip().upper()
            if settle not in {"USDT", "USDC"}:
                raise CryptoTradingConfigError("BYBIT_DEFAULT_SETTLE 仅支持 USDT 或 USDC")
            options["defaultSettle"] = settle
            margin_mode = self._normalize_margin_mode(getattr(self.config, "bybit_margin_mode", "isolated"))
            if margin_mode != "isolated":
                raise CryptoTradingConfigError("Bybit Demo Trading 期货当前仅支持 isolated 保证金模式")

        try:
            import ccxt
        except ImportError as exc:
            raise CryptoTradingConfigError("ccxt 未安装，请运行 pip install ccxt") from exc

        exchange = ccxt.bybit(
            {
                "apiKey": api_key,
                "secret": secret,
                "enableRateLimit": True,
                "timeout": int(getattr(self.config, "crypto_trading_timeout_ms", 10000)),
                "options": options,
            }
        )
        exchange.enable_demo_trading(True)
        return exchange

    def _resolve_symbol(self, symbol: Optional[str], *, required: bool = False) -> Optional[str]:
        instrument = self._resolve_instrument(symbol, required=required)
        return instrument.market_symbol if instrument is not None else None

    def _resolve_instrument(
        self,
        symbol: Optional[str],
        *,
        required: bool = False,
    ) -> Optional[CryptoInstrument]:
        configured_default = (getattr(self.config, "crypto_trading_default_symbol", "") or "").strip()
        if self.exchange_id == "bybit" and not self._is_bybit_swap() and configured_default == "BTC/USDT:USDT":
            configured_default = "BTC/USDT"
        default_symbol = configured_default or self._default_symbol()
        default_type = instrument_type_from_market_symbol(default_symbol)
        default_margin = (
            getattr(self.config, "okx_td_mode", "cross")
            if self.exchange_id == "okx"
            else getattr(self.config, "bybit_margin_mode", "isolated")
        )
        default_instrument = resolve_crypto_instrument(
            default_symbol,
            default_type=default_type,
            venue=self.exchange_id,
            margin_mode=default_margin,
        )
        if default_instrument is None:
            raise CryptoTradingConfigError("CRYPTO_TRADING_DEFAULT_SYMBOL 仅支持 BTC/USDT 现货或永续合约")
        raw = str(symbol or default_symbol).strip()
        if not raw:
            if required:
                raise ValueError("symbol is required")
            return None
        instrument = resolve_crypto_instrument(
            raw,
            default_type=default_type,
            venue=self.exchange_id,
            margin_mode=default_margin,
        )
        if instrument is None:
            raise ValueError("BTC-only 模式仅支持 BTC/USDT 现货或永续合约")
        return instrument

    @staticmethod
    def _is_supported_btc_trading_symbol(symbol: str) -> bool:
        return resolve_crypto_instrument(symbol, default_type="perpetual") is not None

    def _build_order_params(
        self,
        *,
        instrument_type: str,
        td_mode: Optional[str],
        pos_side: Optional[str],
        reduce_only: bool,
        client_order_id: Optional[str],
        extra_params: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        params = dict(extra_params or {})
        if instrument_type == "spot":
            if pos_side:
                raise ValueError("spot orders do not support pos_side")
            if reduce_only:
                raise ValueError("spot orders do not support reduce_only")
            if self.exchange_id == "okx":
                params.setdefault("tdMode", "cash")
            return params
        if self.exchange_id == "okx":
            params.setdefault("tdMode", self._normalize_td_mode(td_mode or getattr(self.config, "okx_td_mode", "cross")))
        elif self._is_bybit_swap():
            margin_mode = self._normalize_margin_mode(td_mode or getattr(self.config, "bybit_margin_mode", "isolated"))
            if margin_mode != "isolated":
                raise ValueError("Bybit futures demo trading only supports isolated td_mode")
            params.setdefault("position_idx", 0)
        if pos_side:
            self._apply_pos_side_param(params, pos_side)
        if reduce_only:
            params["reduceOnly"] = True
        if client_order_id:
            self._apply_client_order_id_param(params, client_order_id)
        return params

    def _get_exchange_id(self) -> str:
        return (getattr(self.config, "crypto_trading_exchange", "okx") or "okx").strip().lower()

    def _default_symbol(self) -> str:
        if self.exchange_id == "bybit" and not self._is_bybit_swap():
            return "BTC/USDT"
        return "BTC/USDT:USDT"

    def _is_bybit_swap(self) -> bool:
        return self.exchange_id == "bybit" and (getattr(self.config, "bybit_default_type", "swap") or "swap").strip().lower() == "swap"

    def _apply_margin_mode_param(self, params: dict[str, Any], margin_mode: str) -> None:
        normalized = self._normalize_margin_mode(margin_mode)
        if self.exchange_id == "okx":
            params["mgnMode"] = normalized
        elif self.exchange_id == "bybit":
            if normalized != "isolated":
                raise ValueError("Bybit futures demo trading only supports isolated margin_mode")
            params["marginMode"] = normalized
        else:
            params["marginMode"] = normalized

    def _apply_pos_side_param(self, params: dict[str, Any], pos_side: str) -> None:
        normalized = self._normalize_pos_side(pos_side)
        if self.exchange_id == "okx":
            params["posSide"] = normalized
        elif self._is_bybit_swap():
            params.setdefault("position_idx", 0)

    def _apply_client_order_id_param(self, params: dict[str, Any], client_order_id: str) -> None:
        normalized = str(client_order_id).strip()
        if not normalized:
            return
        params["clOrdId" if self.exchange_id == "okx" else "orderLinkId"] = normalized

    def _build_set_margin_mode_params(self, margin_mode: str) -> dict[str, Any]:
        if self.exchange_id == "bybit":
            leverage = int(getattr(self.config, "bybit_default_leverage", 2))
            return {"leverage": leverage} if margin_mode == "isolated" else {}
        return {}

    def _prepare_bybit_futures_account(self) -> None:
        load_markets = getattr(self.exchange, "load_markets", None)
        if callable(load_markets):
            load_markets()
        set_position_mode = getattr(self.exchange, "set_position_mode", None)
        if callable(set_position_mode):
            set_position_mode(False)

    def _ensure_trading_enabled(self) -> None:
        if not getattr(self.config, "crypto_trading_enabled", False):
            raise CryptoTradingConfigError("CRYPTO_TRADING_ENABLED=false，禁止真实交易")
        if not is_auth_enabled():
            raise CryptoTradingConfigError("真实交易必须先启用 ADMIN_AUTH_ENABLED=true")

    def _is_dry_run(self) -> bool:
        return bool(getattr(self.config, "crypto_trading_dry_run", True))

    @staticmethod
    def _normalize_order_type(value: str) -> str:
        text = str(value or "").strip().lower()
        if text not in {"market", "limit"}:
            raise ValueError("order_type must be market or limit")
        return text

    @staticmethod
    def _normalize_side(value: str) -> str:
        text = str(value or "").strip().lower()
        if text not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        return text

    @staticmethod
    def _normalize_td_mode(value: str) -> str:
        text = str(value or "").strip().lower()
        if text not in {"cash", "cross", "isolated"}:
            raise ValueError("td_mode must be cash, cross, or isolated")
        return text

    @staticmethod
    def _normalize_margin_mode(value: str) -> str:
        text = str(value or "").strip().lower()
        if text not in {"cross", "isolated"}:
            raise ValueError("margin_mode must be cross or isolated")
        return text

    @staticmethod
    def _normalize_pos_side(value: str) -> str:
        text = str(value or "").strip().lower()
        if text not in {"long", "short", "net"}:
            raise ValueError("pos_side must be long, short, or net")
        return text

    @staticmethod
    def _positive_float(value: Any, field: str) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be a number") from exc
        if parsed <= 0:
            raise ValueError(f"{field} must be positive")
        return parsed

    @classmethod
    def _optional_positive_float(cls, value: Any, field: str) -> Optional[float]:
        if value is None:
            return None
        return cls._positive_float(value, field)

    @staticmethod
    def _safe_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        return {"raw": value}
