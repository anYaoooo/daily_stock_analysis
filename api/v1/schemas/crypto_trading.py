# -*- coding: utf-8 -*-
"""Crypto trading API schemas."""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field


class CryptoTradingStatusResponse(BaseModel):
    exchange: str
    configured: bool
    trading_enabled: bool
    dry_run: bool
    sandbox: bool
    demo_trading: bool = False
    default_symbol: str
    default_type: str
    td_mode: str
    settle: Optional[str] = None


class CryptoBalanceResponse(BaseModel):
    exchange: str
    currency: Optional[str] = None
    balance: Any = Field(default_factory=dict)
    free: Optional[float] = None
    used: Optional[float] = None
    total: Optional[float] = None
    raw: Any = None


class CryptoListResponse(BaseModel):
    exchange: str = "okx"
    items: list[Dict[str, Any]] = Field(default_factory=list)


class CryptoCreateOrderRequest(BaseModel):
    symbol: Optional[str] = Field(None, description="仅支持 BTC/USDT 现货或 BTC/USDT:USDT 永续合约")
    order_type: Literal["market", "limit"] = Field(..., description="market 或 limit")
    side: Literal["buy", "sell"]
    amount: float = Field(..., gt=0)
    price: Optional[float] = Field(None, gt=0)
    td_mode: Optional[Literal["cash", "cross", "isolated"]] = None
    pos_side: Optional[Literal["long", "short", "net"]] = None
    reduce_only: bool = False
    client_order_id: Optional[str] = None
    extra_params: Dict[str, Any] = Field(default_factory=dict)


class CryptoCreateOrderResponse(BaseModel):
    dry_run: bool
    order: Dict[str, Any]


class CryptoCancelOrderRequest(BaseModel):
    order_id: str
    symbol: Optional[str] = Field(None, description="仅支持 BTC/USDT 现货或 BTC/USDT:USDT 永续合约")
    extra_params: Dict[str, Any] = Field(default_factory=dict)


class CryptoCancelOrderResponse(BaseModel):
    dry_run: bool
    cancel: Dict[str, Any]


class CryptoSetLeverageRequest(BaseModel):
    leverage: int = Field(..., gt=0)
    symbol: Optional[str] = Field(None, description="仅支持 BTC/USDT:USDT 永续合约")
    margin_mode: Optional[Literal["cross", "isolated"]] = None
    pos_side: Optional[Literal["long", "short", "net"]] = None


class CryptoSetLeverageResponse(BaseModel):
    dry_run: bool
    leverage: Dict[str, Any]


class CryptoSetMarginModeRequest(BaseModel):
    margin_mode: Literal["cross", "isolated"]
    symbol: Optional[str] = Field(None, description="仅支持 BTC/USDT:USDT 永续合约")


class CryptoSetMarginModeResponse(BaseModel):
    dry_run: bool
    margin_mode: Dict[str, Any]
