# -*- coding: utf-8 -*-
"""
Crypto market data source.

This fetcher handles crypto symbols that the main routing layer has already
reserved for a dedicated provider, such as BTC, BTCUSDT, and BTC-USD.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import requests

from .base import BaseFetcher, DataFetchError, STANDARD_COLUMNS
from .realtime_types import RealtimeSource, UnifiedRealtimeQuote, safe_float, safe_int

logger = logging.getLogger(__name__)

_BINANCE_BASE_URL = "https://api.binance.com"
_QUOTE_ASSETS = ("USDT", "USD", "USDC", "FDUSD", "BUSD")
_PERIOD_TO_BINANCE_INTERVAL = {
    "hourly": "1h",
    "four_hour": "4h",
    "daily": "1d",
    "weekly": "1w",
    "monthly": "1M",
}
_SUPPORTED_BASE_ASSETS = {
    "BTC": "Bitcoin",
    "ETH": "Ethereum",
    "BNB": "BNB",
    "SOL": "Solana",
    "XRP": "XRP",
    "DOGE": "Dogecoin",
    "ADA": "Cardano",
    "AVAX": "Avalanche",
    "DOT": "Polkadot",
    "LINK": "Chainlink",
    "LTC": "Litecoin",
    "BCH": "Bitcoin Cash",
    "TRX": "TRON",
    "TON": "Toncoin",
}


def _normalize_crypto_symbol(stock_code: str) -> Optional[str]:
    """Return a Binance USDT symbol for supported crypto codes."""
    code = (stock_code or "").strip().upper()
    if not code:
        return None

    code = code.removeprefix("CRYPTO:")
    code = code.replace("-", "").replace("/", "").replace("_", "")

    if not re.fullmatch(r"[A-Z0-9]{2,20}", code):
        return None

    for quote in _QUOTE_ASSETS:
        if code.endswith(quote) and len(code) > len(quote):
            base = code[: -len(quote)]
            return f"{base}USDT" if base in _SUPPORTED_BASE_ASSETS else None

    if code in _SUPPORTED_BASE_ASSETS:
        return f"{code}USDT"

    return None


def is_crypto_code(stock_code: str) -> bool:
    """Return True when the code is a supported crypto symbol."""
    return _normalize_crypto_symbol(stock_code) is not None


def crypto_display_name(stock_code: str) -> str:
    """Return a human-friendly display name for a supported crypto symbol."""
    symbol = _normalize_crypto_symbol(stock_code)
    if not symbol:
        return (stock_code or "").strip().upper()
    base = symbol.removesuffix("USDT")
    name = _SUPPORTED_BASE_ASSETS.get(base, base)
    return f"{name} ({base})"


class CryptoFetcher(BaseFetcher):
    """Binance-backed crypto data source."""

    name = "CryptoFetcher"
    priority = 5

    def _to_binance_symbol(self, stock_code: str) -> str:
        symbol = _normalize_crypto_symbol(stock_code)
        if not symbol:
            raise DataFetchError(f"[CryptoFetcher] Unsupported crypto symbol: {stock_code}")
        return symbol

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        symbol = self._to_binance_symbol(stock_code)
        start_ms = int(datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
        end_ms = int(datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)

        return self._fetch_kline_rows(symbol=symbol, interval="1d", start_ms=start_ms, end_ms=end_ms)

    def _fetch_kline_rows(
        self,
        *,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        limit: int = 1000,
    ) -> pd.DataFrame:
        try:
            self.random_sleep(0.2, 0.6)
            resp = requests.get(
                f"{_BINANCE_BASE_URL}/api/v3/klines",
                params={
                    "symbol": symbol,
                    "interval": interval,
                    "startTime": start_ms,
                    "endTime": end_ms,
                    "limit": limit,
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise DataFetchError(f"[CryptoFetcher] Binance kline request failed for {symbol}: {exc}") from exc

        if not data:
            raise DataFetchError(f"[CryptoFetcher] No kline data returned for {symbol}")

        return pd.DataFrame(
            data,
            columns=[
                "open_time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "close_time",
                "quote_volume",
                "trade_count",
                "taker_buy_base_volume",
                "taker_buy_quote_volume",
                "ignore",
            ],
        )

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        if df.empty:
            return df

        df = df.copy()
        df["date"] = pd.to_datetime(df["open_time"], unit="ms", utc=True).dt.tz_convert(None)
        df["amount"] = pd.to_numeric(df["quote_volume"], errors="coerce")
        df["code"] = stock_code

        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df["pct_chg"] = df["close"].pct_change() * 100
        df["pct_chg"] = df["pct_chg"].fillna(0).round(2)

        keep_cols = ["code"] + STANDARD_COLUMNS
        return df[[col for col in keep_cols if col in df.columns]]

    def get_kline_data(self, stock_code: str, period: str = "daily", days: int = 30) -> pd.DataFrame:
        """Fetch crypto K-line data for Web chart periods."""
        interval = _PERIOD_TO_BINANCE_INTERVAL.get(period)
        if interval is None:
            supported = ", ".join(sorted(_PERIOD_TO_BINANCE_INTERVAL))
            raise DataFetchError(f"[CryptoFetcher] Unsupported kline period: {period}; supported: {supported}")

        days = max(1, int(days or 1))
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=days)
        raw_df = self._fetch_kline_rows(
            symbol=self._to_binance_symbol(stock_code),
            interval=interval,
            start_ms=int(start_dt.timestamp() * 1000),
            end_ms=int(end_dt.timestamp() * 1000),
        )
        df = self._normalize_data(raw_df, stock_code)
        df = self._clean_data(df)
        return self._calculate_indicators(df)

    def get_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        try:
            symbol = self._to_binance_symbol(stock_code)
        except DataFetchError:
            return None

        try:
            self.random_sleep(0.2, 0.5)
            resp = requests.get(
                f"{_BINANCE_BASE_URL}/api/v3/ticker/24hr",
                params={"symbol": symbol},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("[CryptoFetcher] Realtime quote failed for %s: %s", stock_code, exc)
            return None

        price = safe_float(data.get("lastPrice"))
        if price is None or price <= 0:
            return None

        close_time = safe_int(data.get("closeTime"))
        provider_timestamp = None
        if close_time:
            provider_timestamp = datetime.fromtimestamp(close_time / 1000, tz=timezone.utc).isoformat()

        return UnifiedRealtimeQuote(
            code=symbol.removesuffix("USDT"),
            name=crypto_display_name(symbol),
            source=RealtimeSource.BINANCE,
            provider_timestamp=provider_timestamp,
            price=price,
            change_pct=safe_float(data.get("priceChangePercent")),
            change_amount=safe_float(data.get("priceChange")),
            volume=safe_int(data.get("volume")),
            amount=safe_float(data.get("quoteVolume")),
            amplitude=None,
            open_price=safe_float(data.get("openPrice")),
            high=safe_float(data.get("highPrice")),
            low=safe_float(data.get("lowPrice")),
            pre_close=safe_float(data.get("prevClosePrice")),
        )

    def get_stock_name(self, stock_code: str) -> Optional[str]:
        if not is_crypto_code(stock_code):
            return None
        return crypto_display_name(stock_code)
