# -*- coding: utf-8 -*-
"""Cryptocurrency market data fetcher.

The first implementation uses Binance public market-data endpoints because
they require no API key and expose both ticker and candlestick data.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pandas as pd
import requests

from .base import BaseFetcher, DataFetchError
from .realtime_types import RealtimeSource, UnifiedRealtimeQuote, safe_float, safe_int

logger = logging.getLogger(__name__)

_BINANCE_BASE_URL = "https://api.binance.com"
_HTTP_TIMEOUT_SECONDS = 10

_BINANCE_INTERVAL_BY_PERIOD = {
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
    """Normalize common BTC symbols to Binance spot symbols."""
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


def is_crypto_code(code: str) -> bool:
    return normalize_crypto_symbol(code) is not None


def crypto_display_name(code: str) -> str:
    symbol = normalize_crypto_symbol(code) or ""
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    return _SUPPORTED_BASE_ASSETS.get(base, base or code)


class CryptoFetcher(BaseFetcher):
    """Fetch BTC market data from Binance public endpoints."""

    name = "CryptoFetcher"
    priority = 1

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return self._fetch_kline_rows(stock_code, start_date, end_date, period="daily")

    def _fetch_kline_rows(self, stock_code: str, start_date: str, end_date: str, *, period: str) -> pd.DataFrame:
        symbol = normalize_crypto_symbol(stock_code)
        if not symbol:
            raise DataFetchError(f"CryptoFetcher unsupported symbol: {stock_code}")
        interval = _BINANCE_INTERVAL_BY_PERIOD.get(period)
        if interval is None:
            supported = ", ".join(sorted(_BINANCE_INTERVAL_BY_PERIOD))
            raise DataFetchError(f"CryptoFetcher unsupported period: {period}; supported: {supported}")

        try:
            start_ms = _date_to_millis(start_date)
            # Binance endTime is inclusive in practice; use the end-of-day boundary.
            end_ms = _date_to_millis(end_date, end_of_day=True)
        except ValueError as exc:
            raise DataFetchError(f"Invalid date range: {start_date} ~ {end_date}") from exc

        response = requests.get(
            f"{_BINANCE_BASE_URL}/api/v3/klines",
            params={
                "symbol": symbol,
                "interval": interval,
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": 1000,
            },
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list) or not rows:
            raise DataFetchError(f"Binance returned empty kline data for {symbol}")
        raw_df = pd.DataFrame(rows)
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
                "amount": df.iloc[:, 7],
            }
        )
        for column in ("open", "high", "low", "close", "volume", "amount"):
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
        normalized["pct_chg"] = normalized["close"].pct_change().fillna(0.0) * 100
        return normalized

    def get_kline_data(self, stock_code: str, period: str = "daily", days: int = 30) -> pd.DataFrame:
        """Fetch native Binance candlesticks for BTC supported periods."""
        if period not in _BINANCE_INTERVAL_BY_PERIOD:
            supported = ", ".join(sorted(_BINANCE_INTERVAL_BY_PERIOD))
            raise DataFetchError(f"CryptoFetcher unsupported period: {period}; supported: {supported}")

        end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        start_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        raw_df = self._fetch_kline_rows(stock_code, start_date, end_date, period=period)
        df = self._normalize_data(raw_df, stock_code)
        df = self._clean_data(df)
        return self._calculate_indicators(df)

    def get_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        symbol = normalize_crypto_symbol(stock_code)
        if not symbol:
            return None

        response = requests.get(
            f"{_BINANCE_BASE_URL}/api/v3/ticker/24hr",
            params={"symbol": symbol},
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise DataFetchError(f"Binance returned invalid ticker payload for {symbol}")

        price = safe_float(payload.get("lastPrice"))
        if price is None or price <= 0:
            return None

        provider_timestamp = _millis_to_iso(payload.get("closeTime"))
        return UnifiedRealtimeQuote(
            code=symbol,
            name=crypto_display_name(symbol),
            source=RealtimeSource.BINANCE,
            provider_timestamp=provider_timestamp,
            price=price,
            change_pct=safe_float(payload.get("priceChangePercent")),
            change_amount=safe_float(payload.get("priceChange")),
            volume=safe_int(payload.get("volume")),
            amount=safe_float(payload.get("quoteVolume")),
            open_price=safe_float(payload.get("openPrice")),
            high=safe_float(payload.get("highPrice")),
            low=safe_float(payload.get("lowPrice")),
            pre_close=safe_float(payload.get("prevClosePrice")),
        )

    def get_stock_name(self, stock_code: str) -> str:
        return crypto_display_name(stock_code)


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
