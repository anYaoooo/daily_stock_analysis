# -*- coding: utf-8 -*-
"""Cryptocurrency market data fetcher backed by CCXT."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pandas as pd
import requests

from .base import BaseFetcher, DataFetchError
from .realtime_types import RealtimeSource, UnifiedRealtimeQuote, safe_float, safe_int

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_SECONDS = 10

_CCXT_EXCHANGE_ID = "okx"
_OKX_REST_BASE_URL = "https://www.okx.com"
_CCXT_TIMEFRAME_BY_PERIOD = {
    "hourly": "1h",
    "four_hour": "4h",
    "daily": "1d",
    "weekly": "1w",
    "monthly": "1M",
}

_SUPPORTED_BASE_ASSETS = {
    "BTC": "Bitcoin",
}

_QUOTE_ALIASES = {
    "USD": "USDT",
    "USDT": "USDT",
}


def normalize_crypto_symbol(code: str) -> Optional[str]:
    """Normalize common BTC symbols to the canonical compact symbol."""
    raw = (code or "").strip().upper()
    if not raw:
        return None

    compact = raw.replace("-", "").replace("/", "").replace("_", "")
    if compact in _SUPPORTED_BASE_ASSETS:
        return f"{compact}USDT"

    for quote in _QUOTE_ALIASES:
        if compact.endswith(quote):
            base = compact[: -len(quote)]
            if base in _SUPPORTED_BASE_ASSETS:
                return f"{base}{_QUOTE_ALIASES[quote]}"
    return None


def normalize_crypto_market_symbol(code: str) -> Optional[str]:
    """Normalize common BTC symbols to the CCXT market symbol format."""
    symbol = normalize_crypto_symbol(code)
    if not symbol:
        return None
    if symbol.endswith("USDT"):
        return f"{symbol[:-4]}/USDT"
    return symbol


def is_crypto_code(code: str) -> bool:
    return normalize_crypto_symbol(code) is not None


def crypto_display_name(code: str) -> str:
    symbol = normalize_crypto_symbol(code) or ""
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    return _SUPPORTED_BASE_ASSETS.get(base, base or code)


class CryptoFetcher(BaseFetcher):
    """Fetch BTC market data through CCXT public market-data methods."""

    name = "CryptoFetcher"
    priority = 1

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return self._fetch_kline_rows(stock_code, start_date, end_date, period="daily")

    def _fetch_kline_rows(self, stock_code: str, start_date: str, end_date: str, *, period: str) -> pd.DataFrame:
        market_symbol = normalize_crypto_market_symbol(stock_code)
        if not market_symbol:
            raise DataFetchError(f"CryptoFetcher unsupported symbol: {stock_code}")
        timeframe = _CCXT_TIMEFRAME_BY_PERIOD.get(period)
        if timeframe is None:
            supported = ", ".join(sorted(_CCXT_TIMEFRAME_BY_PERIOD))
            raise DataFetchError(f"CryptoFetcher unsupported period: {period}; supported: {supported}")

        try:
            start_ms = _date_to_millis(start_date)
            end_ms = _date_to_millis(end_date, end_of_day=True)
        except ValueError as exc:
            raise DataFetchError(f"Invalid date range: {start_date} ~ {end_date}") from exc

        exchange = self._create_exchange()
        try:
            rows = exchange.fetch_ohlcv(
                market_symbol,
                timeframe=timeframe,
                since=start_ms,
                limit=1000,
            )
        except Exception as exc:
            raise DataFetchError(f"CCXT {_CCXT_EXCHANGE_ID} returned kline error for {market_symbol}: {exc}") from exc
        if not isinstance(rows, list) or not rows:
            raise DataFetchError(f"CCXT {_CCXT_EXCHANGE_ID} returned empty kline data for {market_symbol}")
        filtered_rows = [row for row in rows if row and row[0] <= end_ms]
        if not filtered_rows:
            raise DataFetchError(f"CCXT {_CCXT_EXCHANGE_ID} returned no kline data in range for {market_symbol}")
        raw_df = pd.DataFrame(filtered_rows)
        raw_df.attrs["period"] = period
        return raw_df

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount", "pct_chg"])

        period = str(df.attrs.get("period") or "daily")
        open_times = pd.to_datetime(df.iloc[:, 0], unit="ms", utc=True)
        date_values = (
            open_times.dt.strftime("%Y-%m-%d %H:%M")
            if period in {"hourly", "four_hour"}
            else open_times.dt.strftime("%Y-%m-%d")
        )
        normalized = pd.DataFrame(
            {
                "date": date_values,
                "open": df.iloc[:, 1],
                "high": df.iloc[:, 2],
                "low": df.iloc[:, 3],
                "close": df.iloc[:, 4],
                "volume": df.iloc[:, 5],
                "amount": pd.to_numeric(df.iloc[:, 4], errors="coerce") * pd.to_numeric(df.iloc[:, 5], errors="coerce"),
            }
        )
        for column in ("open", "high", "low", "close", "volume", "amount"):
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
        normalized["pct_chg"] = normalized["close"].pct_change().fillna(0.0) * 100
        return normalized

    def get_kline_data(self, stock_code: str, period: str = "daily", days: int = 30) -> pd.DataFrame:
        """Fetch native exchange candlesticks for BTC supported periods."""
        if period not in _CCXT_TIMEFRAME_BY_PERIOD:
            supported = ", ".join(sorted(_CCXT_TIMEFRAME_BY_PERIOD))
            raise DataFetchError(f"CryptoFetcher unsupported period: {period}; supported: {supported}")

        end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        start_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        raw_df = self._fetch_kline_rows(stock_code, start_date, end_date, period=period)
        df = self._normalize_data(raw_df, stock_code)
        df = self._clean_data(df)
        return self._calculate_indicators(df)

    def get_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        symbol = normalize_crypto_symbol(stock_code)
        market_symbol = normalize_crypto_market_symbol(stock_code)
        if not symbol or not market_symbol:
            return None

        try:
            exchange = self._create_exchange()
            payload = exchange.fetch_ticker(market_symbol)
        except Exception as exc:
            logger.warning(
                "CCXT %s 获取 %s 实时行情失败，尝试 OKX REST 兜底: %s",
                _CCXT_EXCHANGE_ID,
                market_symbol,
                exc,
            )
            payload = self._fetch_okx_rest_ticker(symbol, exc)
        if not isinstance(payload, dict):
            raise DataFetchError(f"CCXT {_CCXT_EXCHANGE_ID} returned invalid ticker payload for {market_symbol}")

        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        price = (
            safe_float(payload.get("last"))
            or safe_float(payload.get("lastPrice"))
            or safe_float(info.get("lastPrice"))
        )
        if price is None or price <= 0:
            return None

        provider_timestamp = _millis_to_iso(
            payload.get("timestamp")
            or payload.get("closeTime")
            or info.get("closeTime")
        )
        return UnifiedRealtimeQuote(
            code=symbol,
            name=crypto_display_name(symbol),
            source=RealtimeSource.OKX,
            provider_timestamp=provider_timestamp,
            price=price,
            change_pct=(
                safe_float(payload.get("percentage"))
                or safe_float(payload.get("priceChangePercent"))
                or safe_float(info.get("priceChangePercent"))
            ),
            change_amount=(
                safe_float(payload.get("change"))
                or safe_float(payload.get("priceChange"))
                or safe_float(info.get("priceChange"))
            ),
            volume=safe_int(payload.get("baseVolume") or payload.get("volume") or info.get("volume")),
            amount=safe_float(payload.get("quoteVolume") or info.get("quoteVolume")),
            open_price=safe_float(payload.get("open") or payload.get("openPrice") or info.get("openPrice")),
            high=safe_float(payload.get("high") or payload.get("highPrice") or info.get("highPrice")),
            low=safe_float(payload.get("low") or payload.get("lowPrice") or info.get("lowPrice")),
            pre_close=safe_float(
                payload.get("previousClose")
                or payload.get("prevClosePrice")
                or info.get("prevClosePrice")
            ),
        )

    def get_stock_name(self, stock_code: str) -> str:
        return crypto_display_name(stock_code)

    @staticmethod
    def _fetch_okx_rest_ticker(symbol: str, original_error: Exception) -> dict:
        inst_id = _to_okx_inst_id(symbol)
        url = f"{_OKX_REST_BASE_URL}/api/v5/market/ticker"
        try:
            response = requests.get(
                url,
                params={"instId": inst_id},
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
            ticker = _extract_okx_ticker(payload)
            if ticker is None:
                raise DataFetchError(f"OKX REST returned invalid ticker payload for {inst_id}")
            logger.info("OKX REST 兜底获取 %s 实时行情成功", inst_id)
            return _okx_ticker_to_unified_payload(ticker)
        except Exception as exc:
            raise DataFetchError(
                f"CCXT {_CCXT_EXCHANGE_ID} returned ticker error for {symbol}: {original_error}; "
                f"OKX REST fallback failed: {exc}"
            ) from original_error

    @staticmethod
    def _create_exchange() -> Any:
        try:
            import ccxt
        except ImportError as exc:
            raise DataFetchError("ccxt 未安装，请运行 pip install ccxt") from exc

        exchange_class = getattr(ccxt, _CCXT_EXCHANGE_ID, None)
        if exchange_class is None:
            raise DataFetchError(f"ccxt 不支持交易所: {_CCXT_EXCHANGE_ID}")
        return exchange_class(
            {
                "enableRateLimit": True,
                "timeout": _HTTP_TIMEOUT_SECONDS * 1000,
            }
        )


def _date_to_millis(date_text: str, *, end_of_day: bool = False) -> int:
    dt = datetime.strptime(date_text, "%Y-%m-%d")
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=999000)
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _millis_to_iso(value: Any) -> Optional[str]:
    millis = safe_int(value)
    if millis is None:
        return None
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).isoformat()


def _to_okx_inst_id(symbol: str) -> str:
    normalized = normalize_crypto_symbol(symbol) or symbol
    if normalized.endswith("USDT"):
        return f"{normalized[:-4]}-USDT"
    return normalized.replace("/", "-")


def _extract_okx_ticker(payload: Any) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return None
    return data[0]


def _okx_ticker_to_unified_payload(ticker: dict) -> dict:
    last = safe_float(ticker.get("last"))
    open_24h = safe_float(ticker.get("open24h"))
    change = last - open_24h if last is not None and open_24h not in (None, 0) else None
    change_pct = (change / open_24h * 100) if change is not None and open_24h else None
    return {
        "last": last,
        "change": change,
        "percentage": change_pct,
        "baseVolume": safe_float(ticker.get("vol24h")),
        "quoteVolume": safe_float(ticker.get("volCcy24h")),
        "open": open_24h,
        "high": safe_float(ticker.get("high24h")),
        "low": safe_float(ticker.get("low24h")),
        "previousClose": open_24h,
        "timestamp": safe_int(ticker.get("ts")),
        "info": ticker,
    }
