# -*- coding: utf-8 -*-
"""Crypto trading endpoints backed by configured CCXT private APIs."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from api.v1.schemas.common import ErrorResponse
from api.v1.schemas.crypto_trading import (
    CryptoBalanceResponse,
    CryptoCancelOrderRequest,
    CryptoCancelOrderResponse,
    CryptoCreateOrderRequest,
    CryptoCreateOrderResponse,
    CryptoListResponse,
    CryptoSetLeverageRequest,
    CryptoSetLeverageResponse,
    CryptoSetMarginModeRequest,
    CryptoSetMarginModeResponse,
    CryptoTradingStatusResponse,
)
from src.config import get_config
from src.services.crypto_trading_service import CryptoTradingConfigError, CryptoTradingService

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/status",
    response_model=CryptoTradingStatusResponse,
    summary="获取加密货币交易配置状态",
)
def get_crypto_trading_status() -> CryptoTradingStatusResponse:
    config = get_config()
    exchange = (getattr(config, "crypto_trading_exchange", "okx") or "okx").strip().lower()
    default_symbol = str(getattr(config, "crypto_trading_default_symbol", "BTC/USDT:USDT") or "BTC/USDT:USDT")
    if exchange == "bybit":
        configured = all(
            bool((getattr(config, field, "") or "").strip())
            for field in ("bybit_api_key", "bybit_secret")
        ) and bool(getattr(config, "bybit_demo_trading", False))
        sandbox = False
        demo_trading = bool(getattr(config, "bybit_demo_trading", False))
        default_type = str(getattr(config, "bybit_default_type", "swap"))
        td_mode = str(getattr(config, "bybit_margin_mode", "isolated"))
        settle = str(getattr(config, "bybit_default_settle", "USDT"))
        if default_type.strip().lower() == "spot" and default_symbol == "BTC/USDT:USDT":
            default_symbol = "BTC/USDT"
    else:
        exchange = "okx"
        configured = all(
            bool((getattr(config, field, "") or "").strip())
            for field in ("okx_api_key", "okx_secret", "okx_password")
        )
        sandbox = bool(getattr(config, "okx_sandbox", False))
        demo_trading = False
        default_type = str(getattr(config, "okx_default_type", "swap"))
        td_mode = str(getattr(config, "okx_td_mode", "cross"))
        settle = None
    return CryptoTradingStatusResponse(
        exchange=exchange,
        configured=configured,
        trading_enabled=bool(getattr(config, "crypto_trading_enabled", False)),
        dry_run=bool(getattr(config, "crypto_trading_dry_run", True)),
        sandbox=sandbox,
        demo_trading=demo_trading,
        default_symbol=default_symbol,
        default_type=default_type,
        td_mode=td_mode,
        settle=settle,
    )


@router.get(
    "/balance",
    response_model=CryptoBalanceResponse,
    responses={400: {"description": "配置或参数错误", "model": ErrorResponse}},
    summary="查询加密货币账户余额",
)
def get_crypto_balance(
    currency: Optional[str] = Query(None, description="可选币种，如 USDT/BTC"),
) -> CryptoBalanceResponse:
    try:
        return CryptoBalanceResponse(**CryptoTradingService().fetch_balance(currency=currency))
    except CryptoTradingConfigError as exc:
        raise HTTPException(status_code=400, detail={"error": "config_error", "message": str(exc)})
    except Exception as exc:
        logger.error("查询加密货币余额失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.get(
    "/positions",
    response_model=CryptoListResponse,
    responses={400: {"description": "配置或参数错误", "model": ErrorResponse}},
    summary="查询加密货币持仓",
)
def get_crypto_positions(
    symbol: Optional[str] = Query(None, description="默认 BTC/USDT:USDT"),
) -> CryptoListResponse:
    try:
        service = CryptoTradingService()
        return CryptoListResponse(exchange=service.exchange_id, items=service.fetch_positions(symbol=symbol))
    except CryptoTradingConfigError as exc:
        raise HTTPException(status_code=400, detail={"error": "config_error", "message": str(exc)})
    except Exception as exc:
        logger.error("查询加密货币持仓失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.get(
    "/orders/open",
    response_model=CryptoListResponse,
    responses={400: {"description": "配置或参数错误", "model": ErrorResponse}},
    summary="查询加密货币当前挂单",
)
def get_crypto_open_orders(
    symbol: Optional[str] = Query(None, description="默认 BTC/USDT:USDT"),
) -> CryptoListResponse:
    try:
        service = CryptoTradingService()
        return CryptoListResponse(exchange=service.exchange_id, items=service.fetch_open_orders(symbol=symbol))
    except CryptoTradingConfigError as exc:
        raise HTTPException(status_code=400, detail={"error": "config_error", "message": str(exc)})
    except Exception as exc:
        logger.error("查询加密货币挂单失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.get(
    "/orders/{order_id}",
    response_model=dict,
    responses={400: {"description": "配置或参数错误", "model": ErrorResponse}},
    summary="查询加密货币单个订单",
)
def get_crypto_order(
    order_id: str,
    symbol: Optional[str] = Query(None, description="默认 BTC/USDT:USDT"),
) -> dict:
    try:
        return CryptoTradingService().fetch_order(order_id=order_id, symbol=symbol)
    except CryptoTradingConfigError as exc:
        raise HTTPException(status_code=400, detail={"error": "config_error", "message": str(exc)})
    except Exception as exc:
        logger.error("查询加密货币订单失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.post(
    "/orders",
    response_model=CryptoCreateOrderResponse,
    responses={400: {"description": "配置或参数错误", "model": ErrorResponse}},
    summary="创建加密货币订单",
    description="默认 dry-run，仅返回订单预览；需 CRYPTO_TRADING_ENABLED=true 且 CRYPTO_TRADING_DRY_RUN=false 才会真实下单。",
)
def create_crypto_order(request: CryptoCreateOrderRequest) -> CryptoCreateOrderResponse:
    try:
        result = CryptoTradingService().create_order(
            symbol=request.symbol,
            order_type=request.order_type,
            side=request.side,
            amount=request.amount,
            price=request.price,
            td_mode=request.td_mode,
            pos_side=request.pos_side,
            reduce_only=request.reduce_only,
            client_order_id=request.client_order_id,
            extra_params=request.extra_params,
        )
        return CryptoCreateOrderResponse(**result)
    except (CryptoTradingConfigError, ValueError) as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_request", "message": str(exc)})
    except Exception as exc:
        logger.error("创建加密货币订单失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.delete(
    "/orders",
    response_model=CryptoCancelOrderResponse,
    responses={400: {"description": "配置或参数错误", "model": ErrorResponse}},
    summary="撤销加密货币订单",
)
def cancel_crypto_order(request: CryptoCancelOrderRequest) -> CryptoCancelOrderResponse:
    return _cancel_crypto_order_payload(request)


@router.post(
    "/orders/cancel",
    response_model=CryptoCancelOrderResponse,
    responses={400: {"description": "配置或参数错误", "model": ErrorResponse}},
    summary="撤销加密货币订单",
)
def cancel_crypto_order_post(request: CryptoCancelOrderRequest) -> CryptoCancelOrderResponse:
    return _cancel_crypto_order_payload(request)


def _cancel_crypto_order_payload(request: CryptoCancelOrderRequest) -> CryptoCancelOrderResponse:
    try:
        result = CryptoTradingService().cancel_order(
            order_id=request.order_id,
            symbol=request.symbol,
            extra_params=request.extra_params,
        )
        return CryptoCancelOrderResponse(**result)
    except (CryptoTradingConfigError, ValueError) as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_request", "message": str(exc)})
    except Exception as exc:
        logger.error("撤销加密货币订单失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.post(
    "/leverage",
    response_model=CryptoSetLeverageResponse,
    responses={400: {"description": "配置或参数错误", "model": ErrorResponse}},
    summary="设置加密货币杠杆",
)
def set_crypto_leverage(request: CryptoSetLeverageRequest) -> CryptoSetLeverageResponse:
    try:
        result = CryptoTradingService().set_leverage(
            leverage=request.leverage,
            symbol=request.symbol,
            margin_mode=request.margin_mode,
            pos_side=request.pos_side,
        )
        return CryptoSetLeverageResponse(**result)
    except (CryptoTradingConfigError, ValueError) as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_request", "message": str(exc)})
    except Exception as exc:
        logger.error("设置加密货币杠杆失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.post(
    "/margin-mode",
    response_model=CryptoSetMarginModeResponse,
    responses={400: {"description": "配置或参数错误", "model": ErrorResponse}},
    summary="设置加密货币保证金模式",
)
def set_crypto_margin_mode(request: CryptoSetMarginModeRequest) -> CryptoSetMarginModeResponse:
    try:
        result = CryptoTradingService().set_margin_mode(
            margin_mode=request.margin_mode,
            symbol=request.symbol,
        )
        return CryptoSetMarginModeResponse(**result)
    except (CryptoTradingConfigError, ValueError) as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_request", "message": str(exc)})
    except Exception as exc:
        logger.error("设置加密货币保证金模式失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})
