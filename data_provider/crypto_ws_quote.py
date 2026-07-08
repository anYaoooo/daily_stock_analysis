# -*- coding: utf-8 -*-
"""WebSocket-backed BTC quote cache for low-latency volatility monitoring."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .crypto_fetcher import CryptoFetcher, normalize_crypto_symbol

logger = logging.getLogger(__name__)

_BINANCE_WS_BASE_URL = "wss://stream.binance.com:9443/ws"


def _millis_to_iso(value: Any) -> Optional[str]:
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).isoformat()


class BinanceTickerWebSocketQuoteFetcher:
    """Return Binance ticker prices from a background WebSocket cache.

    The callable interface intentionally mirrors ``CryptoFetcher.get_realtime_quote``
    so it can be injected into ``BTCVolatilityMonitor``. When the WebSocket cache
    is missing or stale, it falls back to the REST fetcher to preserve the current
    runtime contract.
    """

    def __init__(
        self,
        *,
        stale_after_seconds: int = 30,
        reconnect_delay_seconds: int = 5,
        rest_fetcher: Optional[CryptoFetcher] = None,
    ) -> None:
        self.stale_after_seconds = max(1, int(stale_after_seconds or 30))
        self.reconnect_delay_seconds = max(1, int(reconnect_delay_seconds or 5))
        self.rest_fetcher = rest_fetcher or CryptoFetcher()
        self._lock = threading.Lock()
        self._latest: Dict[str, Dict[str, Any]] = {}
        self._threads: Dict[str, threading.Thread] = {}
        self._last_cache_log_at: Dict[str, float] = {}
        self._last_message_log_at: Dict[str, float] = {}
        self._last_invalid_message_log_at: Dict[str, float] = {}
        self._last_rest_fallback_log_at: Dict[str, float] = {}
        self._websockets_unavailable = False

    def __call__(self, symbol: str) -> Any:
        normalized = normalize_crypto_symbol(symbol)
        if not normalized:
            logger.info(
                "[BinanceWS] 无法规范化交易对，使用 REST 行情: input_symbol=%s",
                symbol,
            )
            return self.rest_fetcher.get_realtime_quote(symbol)

        self._ensure_stream(normalized)
        cached = self._fresh_cached_quote(normalized)
        if cached is not None:
            return cached
        self._log_rest_fallback(normalized)
        return self.rest_fetcher.get_realtime_quote(symbol)

    def _fresh_cached_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            quote = dict(self._latest.get(symbol) or {})
        if not quote:
            self._log_cache_status(symbol, "miss")
            return None
        received_at = quote.get("_received_at")
        try:
            age = time.time() - float(received_at)
        except (TypeError, ValueError):
            logger.warning("[BinanceWS] 缓存时间戳无效: symbol=%s received_at=%r", symbol, received_at)
            return None
        if age > self.stale_after_seconds:
            self._log_cache_status(symbol, "stale", age=age)
            return None
        quote.pop("_received_at", None)
        logger.debug(
            "[BinanceWS] 使用 WebSocket 缓存行情: symbol=%s age=%.1fs price=%s",
            symbol,
            age,
            quote.get("price"),
        )
        return quote

    def _ensure_stream(self, symbol: str) -> None:
        if self._websockets_unavailable:
            logger.debug("[BinanceWS] websockets 不可用，跳过启动行情线程: symbol=%s", symbol)
            return
        with self._lock:
            worker = self._threads.get(symbol)
            if worker is not None and worker.is_alive():
                logger.debug(
                    "[BinanceWS] 行情线程已在运行: symbol=%s thread=%s",
                    symbol,
                    worker.name,
                )
                return
            worker = threading.Thread(
                target=self._run_stream_thread,
                args=(symbol,),
                daemon=True,
                name=f"binance-ws-{symbol.lower()}",
            )
            self._threads[symbol] = worker
            logger.info(
                "[BinanceWS] 启动行情线程: symbol=%s thread=%s",
                symbol,
                worker.name,
            )
            worker.start()

    def _run_stream_thread(self, symbol: str) -> None:
        try:
            import websockets  # noqa: F401
        except ImportError:
            logger.warning(
                "websockets 未安装，BTC WebSocket 行情不可用；将继续使用 REST 兜底。"
            )
            self._websockets_unavailable = True
            return

        try:
            logger.info("[BinanceWS] 行情线程进入事件循环: symbol=%s", symbol)
            asyncio.run(self._run_stream(symbol))
        except Exception as exc:  # pragma: no cover - final defensive guard
            logger.warning("[BinanceWS] 行情线程异常退出: symbol=%s error=%s", symbol, exc)

    async def _run_stream(self, symbol: str) -> None:
        import websockets

        stream = f"{symbol.lower()}@ticker"
        url = f"{_BINANCE_WS_BASE_URL}/{stream}"
        while True:
            try:
                logger.info("[BinanceWS] 正在连接行情流: symbol=%s url=%s", symbol, url)
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                ) as websocket:
                    logger.info("[BinanceWS] 行情流已连接: symbol=%s stream=%s", symbol, stream)
                    async for message in websocket:
                        self._handle_message(symbol, message)
            except Exception as exc:
                logger.warning(
                    "[BinanceWS] 行情流断开或连接失败，%s 秒后重连: symbol=%s error=%s",
                    self.reconnect_delay_seconds,
                    symbol,
                    exc,
                )
                await asyncio.sleep(self.reconnect_delay_seconds)

    def _handle_message(self, symbol: str, message: Any) -> None:
        try:
            payload = json.loads(message)
        except (TypeError, json.JSONDecodeError):
            self._log_invalid_message(symbol, "invalid_json", message=message)
            return
        if not isinstance(payload, dict):
            self._log_invalid_message(symbol, "non_dict_payload", message=message)
            return

        raw_price = payload.get("c")
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            self._log_invalid_message(symbol, "invalid_price", message=message)
            return
        if price <= 0:
            self._log_invalid_message(symbol, "non_positive_price", message=message)
            return

        quote = {
            "code": symbol,
            "name": "Bitcoin",
            "source": "binance_ws",
            "provider_timestamp": _millis_to_iso(payload.get("E")),
            "price": price,
            "change_pct": _safe_float(payload.get("P")),
            "change_amount": _safe_float(payload.get("p")),
            "volume": _safe_float(payload.get("v")),
            "amount": _safe_float(payload.get("q")),
            "open_price": _safe_float(payload.get("o")),
            "high": _safe_float(payload.get("h")),
            "low": _safe_float(payload.get("l")),
            "_received_at": time.time(),
        }
        with self._lock:
            first_quote = symbol not in self._latest
            self._latest[symbol] = quote
        now = quote["_received_at"]
        if first_quote:
            logger.info(
                "[BinanceWS] 已收到首条有效行情: symbol=%s price=%s provider_timestamp=%s",
                symbol,
                price,
                quote["provider_timestamp"],
            )
        else:
            self._log_message_status(symbol, price=price, provider_timestamp=quote["provider_timestamp"], now=now)

    def _log_cache_status(self, symbol: str, status: str, *, age: Optional[float] = None) -> None:
        now = time.time()
        if now - self._last_cache_log_at.get(symbol, 0.0) < 30:
            return
        self._last_cache_log_at[symbol] = now
        if status == "stale" and age is not None:
            logger.info(
                "[BinanceWS] WebSocket 缓存已过期: symbol=%s age=%.1fs stale_after=%ss",
                symbol,
                age,
                self.stale_after_seconds,
            )
            return
        logger.info("[BinanceWS] WebSocket 缓存未就绪: symbol=%s status=%s", symbol, status)

    def _log_message_status(
        self,
        symbol: str,
        *,
        price: float,
        provider_timestamp: Optional[str],
        now: float,
    ) -> None:
        if now - self._last_message_log_at.get(symbol, 0.0) < 60:
            logger.debug(
                "[BinanceWS] 收到行情 tick: symbol=%s price=%s provider_timestamp=%s",
                symbol,
                price,
                provider_timestamp,
            )
            return
        self._last_message_log_at[symbol] = now
        logger.info(
            "[BinanceWS] WebSocket 行情运行中: symbol=%s price=%s provider_timestamp=%s",
            symbol,
            price,
            provider_timestamp,
        )

    def _log_invalid_message(self, symbol: str, reason: str, *, message: Any) -> None:
        now = time.time()
        if now - self._last_invalid_message_log_at.get(symbol, 0.0) < 30:
            logger.debug("[BinanceWS] 忽略无效消息: symbol=%s reason=%s message=%r", symbol, reason, message)
            return
        self._last_invalid_message_log_at[symbol] = now
        logger.warning("[BinanceWS] 忽略无效消息: symbol=%s reason=%s message=%r", symbol, reason, message)

    def _log_rest_fallback(self, symbol: str) -> None:
        now = time.time()
        if now - self._last_rest_fallback_log_at.get(symbol, 0.0) < 30:
            logger.debug(
                "[BinanceWS] 暂无可用缓存，使用 REST 行情兜底: symbol=%s stale_after=%ss",
                symbol,
                self.stale_after_seconds,
            )
            return
        self._last_rest_fallback_log_at[symbol] = now
        logger.info(
            "[BinanceWS] 暂无可用缓存，使用 REST 行情兜底: symbol=%s stale_after=%ss",
            symbol,
            self.stale_after_seconds,
        )


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
