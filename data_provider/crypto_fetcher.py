# -*- coding: utf-8 -*-
"""Cryptocurrency market data fetcher backed by CCXT."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import pandas as pd
import requests

from .base import BaseFetcher, DataFetchError
from .realtime_types import RealtimeSource, UnifiedRealtimeQuote, safe_float, safe_int
from src.schemas.crypto_instrument import resolve_crypto_instrument

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_SECONDS = 10
_PROVIDER_COOLDOWN_SECONDS = 300
_CCXT_KLINE_PAGE_SIZE = 300
_DEFAULT_FETCH_BUDGET_SECONDS = 60.0
_DEFAULT_FETCH_MAX_PAGES = 200
_DEFAULT_FETCH_RETRY_COUNT = 2

_CCXT_EXCHANGE_ID = "okx"
_OKX_REST_BASE_URL = "https://www.okx.com"
_BINANCE_REST_BASE_URL = "https://api.binance.com"
_BYBIT_REST_BASE_URL = "https://api.bybit.com"
_CCXT_TIMEFRAME_BY_PERIOD = {
    "hourly": "1h",
    "four_hour": "4h",
    "daily": "1d",
    "weekly": "1w",
    "monthly": "1M",
}
_BINANCE_INTERVAL_BY_PERIOD = {
    "hourly": "1h",
    "four_hour": "4h",
    "daily": "1d",
    "weekly": "1w",
    "monthly": "1M",
}
_BYBIT_INTERVAL_BY_PERIOD = {
    "hourly": "60",
    "four_hour": "240",
    "daily": "D",
    "weekly": "W",
    "monthly": "M",
}

_SUPPORTED_BASE_ASSETS = {
    "BTC": "Bitcoin",
}

def normalize_crypto_symbol(code: str) -> Optional[str]:
    """Normalize common BTC symbols to the canonical compact symbol."""
    instrument = resolve_crypto_instrument(code, default_type="spot")
    return "BTCUSDT" if instrument is not None else None


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

    def __init__(
        self,
        *,
        clock: Optional[Callable[[], float]] = None,
        fetch_budget_seconds: float = _DEFAULT_FETCH_BUDGET_SECONDS,
        fetch_max_pages: int = _DEFAULT_FETCH_MAX_PAGES,
        fetch_retry_count: int = _DEFAULT_FETCH_RETRY_COUNT,
    ) -> None:
        self._clock = clock or time.monotonic
        self._okx_unavailable_until = 0.0
        self._fetch_budget_seconds = max(float(fetch_budget_seconds), 1.0)
        self._fetch_max_pages = max(int(fetch_max_pages), 1)
        self._fetch_retry_count = max(int(fetch_retry_count), 0)

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

        source = "okx"
        try:
            exchange = self._create_exchange()
            rows = self._fetch_ccxt_ohlcv_pages(
                exchange=exchange,
                market_symbol=market_symbol,
                timeframe=timeframe,
                start_ms=start_ms,
                end_ms=end_ms,
            )
        except Exception as exc:
            logger.warning(
                "CCXT %s 获取 %s K 线失败，尝试跨交易所公共接口兜底: %s",
                _CCXT_EXCHANGE_ID,
                market_symbol,
                exc,
            )
            fallback_result = self._fetch_fallback_kline_rows(
                symbol=normalize_crypto_symbol(stock_code) or "",
                period=period,
                start_ms=start_ms,
                end_ms=end_ms,
                original_error=exc,
            )
            if isinstance(fallback_result, tuple):
                rows, source = fallback_result
            else:
                rows = fallback_result
                source = "fallback"
        if not isinstance(rows, list) or not rows:
            raise DataFetchError(f"CCXT {_CCXT_EXCHANGE_ID} returned empty kline data for {market_symbol}")
        unique_rows = {
            int(row[0]): row
            for row in rows
            if row and safe_int(row[0]) is not None
        }
        filtered_rows = [
            unique_rows[timestamp]
            for timestamp in sorted(unique_rows)
            if start_ms <= timestamp <= end_ms
        ]
        if not filtered_rows:
            raise DataFetchError(f"CCXT {_CCXT_EXCHANGE_ID} returned no kline data in range for {market_symbol}")
        raw_df = pd.DataFrame(filtered_rows)
        raw_df.attrs.update(
            {
                "period": period,
                "source": source,
                "venue": source,
                "instrument_type": "spot",
                "canonical_symbol": "BTC-USDT",
                "market_symbol": market_symbol,
                "price_type": "trade",
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return raw_df

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount", "pct_chg"])

        source_attrs = dict(df.attrs)
        period = str(source_attrs.get("period") or "daily")
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
        normalized.attrs.update(source_attrs)
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
        result = self._calculate_indicators(df)
        result.attrs.update(raw_df.attrs)
        return result

    def get_perpetual_kline_data(
        self,
        stock_code: str,
        *,
        period: str = "daily",
        days: int = 30,
        venue: str = "okx",
        margin_mode: str = "isolated",
    ) -> pd.DataFrame:
        """Fetch aligned perpetual trade/mark candles and historical funding events."""

        if period not in _CCXT_TIMEFRAME_BY_PERIOD:
            supported = ", ".join(sorted(_CCXT_TIMEFRAME_BY_PERIOD))
            raise DataFetchError(f"CryptoFetcher unsupported period: {period}; supported: {supported}")
        instrument = resolve_crypto_instrument(
            stock_code,
            default_type="perpetual",
            venue=venue,
            margin_mode=margin_mode,
        )
        if instrument is None or instrument.instrument_type != "perpetual":
            raise DataFetchError(f"CryptoFetcher unsupported perpetual symbol: {stock_code}")

        end_at = datetime.now(timezone.utc)
        start_at = end_at - timedelta(days=max(int(days), 1))
        start_ms = int(start_at.timestamp() * 1000)
        end_ms = int(end_at.timestamp() * 1000)
        timeframe = _CCXT_TIMEFRAME_BY_PERIOD[period]
        exchange = self._create_public_exchange(instrument.venue, instrument_type="perpetual")

        trade_rows = self._fetch_ccxt_ohlcv_pages(
            exchange=exchange,
            market_symbol=instrument.market_symbol,
            timeframe=timeframe,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        mark_rows = self._fetch_ccxt_ohlcv_pages(
            exchange=exchange,
            market_symbol=instrument.market_symbol,
            timeframe=timeframe,
            start_ms=start_ms,
            end_ms=end_ms,
            params={"price": "mark"},
        )
        if not trade_rows or not mark_rows:
            raise DataFetchError(
                f"{instrument.venue} returned incomplete perpetual trade/mark candles for {instrument.market_symbol}"
            )

        trade_by_ts = {int(row[0]): row for row in trade_rows if row and safe_int(row[0]) is not None}
        mark_by_ts = {int(row[0]): row for row in mark_rows if row and safe_int(row[0]) is not None}
        if set(trade_by_ts) != set(mark_by_ts):
            raise DataFetchError(
                f"{instrument.venue} perpetual trade/mark timestamps are incomplete or unsynchronized"
            )

        funding_events = self._fetch_ccxt_funding_history(
            exchange=exchange,
            market_symbol=instrument.market_symbol,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        if end_ms - start_ms >= 8 * 60 * 60 * 1000 and not funding_events:
            raise DataFetchError(
                f"{instrument.venue} returned no historical funding data for {instrument.market_symbol}"
            )

        timestamps = sorted(trade_by_ts)
        raw_trade = pd.DataFrame([trade_by_ts[timestamp] for timestamp in timestamps])
        raw_trade.attrs["period"] = period
        result = self._normalize_data(raw_trade, stock_code)
        mark_columns = ("mark_open", "mark_high", "mark_low", "mark_close")
        for index, column in enumerate(mark_columns, start=1):
            result[column] = [safe_float(mark_by_ts[timestamp][index]) for timestamp in timestamps]
        result["execution_open"] = result["open"]
        result["execution_high"] = result["high"]
        result["execution_low"] = result["low"]
        result["execution_close"] = result["close"]
        result["funding_rates"] = self._funding_rates_by_bar(
            timestamps=timestamps,
            funding_events=funding_events,
            period=period,
        )
        result["funding_complete"] = True
        result = self._clean_data(result)
        result = self._calculate_indicators(result)
        result.attrs.update(
            {
                "source": f"{instrument.venue}_ccxt",
                "venue": instrument.venue,
                "instrument_type": instrument.instrument_type,
                "canonical_symbol": instrument.canonical_symbol,
                "market_symbol": instrument.market_symbol,
                "price_type": "trade_and_mark",
                "funding_event_count": len(funding_events),
                "funding_complete": True,
                "fetched_at": end_at.isoformat(),
            }
        )
        return result

    def _fetch_ccxt_ohlcv_pages(
        self,
        *,
        exchange: Any,
        market_symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
        params: Optional[dict[str, Any]] = None,
    ) -> list:
        rows: list = []
        cursor_ms = int(start_ms)
        deadline = time.monotonic() + self._fetch_budget_seconds
        for _ in range(self._fetch_max_pages):
            if cursor_ms > end_ms:
                break

            def fetch_page() -> Any:
                kwargs: dict[str, Any] = {
                    "timeframe": timeframe,
                    "since": cursor_ms,
                    "limit": _CCXT_KLINE_PAGE_SIZE,
                }
                if params:
                    kwargs["params"] = params
                return exchange.fetch_ohlcv(market_symbol, **kwargs)

            page = self._call_with_retry(fetch_page, deadline=deadline)
            if not isinstance(page, list) or not page:
                break
            rows.extend(page)
            last_timestamp = safe_int(page[-1][0]) if page[-1] else None
            if last_timestamp is None or last_timestamp < cursor_ms:
                raise DataFetchError(f"non-advancing OHLCV cursor for {market_symbol}")
            if last_timestamp >= end_ms or len(page) < _CCXT_KLINE_PAGE_SIZE:
                break
            cursor_ms = last_timestamp + 1
        else:
            raise DataFetchError(f"OHLCV pagination exceeded {self._fetch_max_pages} pages for {market_symbol}")
        return rows

    def _fetch_ccxt_funding_history(
        self,
        *,
        exchange: Any,
        market_symbol: str,
        start_ms: int,
        end_ms: int,
    ) -> list[tuple[int, float]]:
        fetch_history = getattr(exchange, "fetch_funding_rate_history", None)
        if not callable(fetch_history):
            raise DataFetchError("configured exchange does not support funding-rate history")

        events: list[tuple[int, float]] = []
        cursor_ms = int(start_ms)
        deadline = time.monotonic() + self._fetch_budget_seconds
        for _ in range(self._fetch_max_pages):
            if cursor_ms > end_ms:
                break
            page = self._call_with_retry(
                lambda: fetch_history(market_symbol, since=cursor_ms, limit=1000),
                deadline=deadline,
            )
            if not isinstance(page, list) or not page:
                break
            last_timestamp: Optional[int] = None
            for item in page:
                if not isinstance(item, dict):
                    continue
                timestamp = safe_int(item.get("timestamp"))
                rate = safe_float(item.get("fundingRate"))
                if timestamp is None or rate is None or timestamp < start_ms or timestamp > end_ms:
                    continue
                events.append((timestamp, rate))
                last_timestamp = max(last_timestamp or timestamp, timestamp)
            if last_timestamp is None or len(page) < 1000:
                break
            if last_timestamp < cursor_ms:
                raise DataFetchError(f"non-advancing funding cursor for {market_symbol}")
            cursor_ms = last_timestamp + 1
        else:
            raise DataFetchError(f"funding pagination exceeded {self._fetch_max_pages} pages for {market_symbol}")
        return sorted(set(events))

    def _call_with_retry(self, operation: Callable[[], Any], *, deadline: float) -> Any:
        last_error: Optional[Exception] = None
        for attempt in range(self._fetch_retry_count + 1):
            if time.monotonic() >= deadline:
                raise DataFetchError("crypto market-data fetch exceeded its total time budget") from last_error
            try:
                return operation()
            except Exception as exc:
                last_error = exc
                if attempt >= self._fetch_retry_count:
                    raise
                remaining = max(deadline - time.monotonic(), 0.0)
                if remaining <= 0:
                    break
                time.sleep(min(0.25 * (2 ** attempt), remaining))
        raise DataFetchError("crypto market-data fetch failed after bounded retries") from last_error

    @staticmethod
    def _funding_rates_by_bar(
        *,
        timestamps: list[int],
        funding_events: list[tuple[int, float]],
        period: str,
    ) -> list[tuple[float, ...]]:
        duration_ms = {
            "hourly": 60 * 60 * 1000,
            "four_hour": 4 * 60 * 60 * 1000,
            "daily": 24 * 60 * 60 * 1000,
            "weekly": 7 * 24 * 60 * 60 * 1000,
            "monthly": 31 * 24 * 60 * 60 * 1000,
        }[period]
        return [
            tuple(rate for event_ts, rate in funding_events if timestamp <= event_ts < timestamp + duration_ms)
            for timestamp in timestamps
        ]

    def get_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        symbol = normalize_crypto_symbol(stock_code)
        market_symbol = normalize_crypto_market_symbol(stock_code)
        if not symbol or not market_symbol:
            return None

        source = RealtimeSource.OKX
        payload: Optional[dict] = None
        okx_error: Exception = DataFetchError("OKX provider is cooling down")
        now = self._clock()
        if now >= self._okx_unavailable_until:
            try:
                exchange = self._create_exchange()
                payload = exchange.fetch_ticker(market_symbol)
                self._okx_unavailable_until = 0.0
            except Exception as exc:
                logger.warning(
                    "CCXT %s 获取 %s 实时行情失败，尝试 OKX REST 兜底: %s",
                    _CCXT_EXCHANGE_ID,
                    market_symbol,
                    exc,
                )
                try:
                    payload = self._fetch_okx_rest_ticker(symbol, exc)
                    self._okx_unavailable_until = 0.0
                except Exception as okx_rest_error:
                    okx_error = okx_rest_error
                    self._okx_unavailable_until = now + _PROVIDER_COOLDOWN_SECONDS
        else:
            logger.debug(
                "OKX 行情处于故障冷却期，直接尝试跨交易所兜底: remaining=%.1fs",
                self._okx_unavailable_until - now,
            )
        if payload is None:
            payload, source = self._fetch_cross_exchange_ticker(symbol, okx_error)
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
            source=source,
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

    @classmethod
    def _fetch_cross_exchange_ticker(
        cls,
        symbol: str,
        original_error: Exception,
    ) -> tuple[dict, RealtimeSource]:
        errors = [f"OKX: {original_error}"]
        for provider, fetcher, source in (
            ("Binance", cls._fetch_binance_rest_ticker, RealtimeSource.BINANCE),
            ("Bybit", cls._fetch_bybit_rest_ticker, RealtimeSource.BYBIT),
        ):
            try:
                payload = fetcher(symbol)
                logger.info("%s REST 兜底获取 %s 实时行情成功", provider, symbol)
                return payload, source
            except Exception as exc:
                errors.append(f"{provider}: {exc}")
        raise DataFetchError("BTC 实时行情全部数据源失败；" + "; ".join(errors)) from original_error

    @staticmethod
    def _fetch_binance_rest_ticker(symbol: str) -> dict:
        response = requests.get(
            f"{_BINANCE_REST_BASE_URL}/api/v3/ticker/24hr",
            params={"symbol": normalize_crypto_symbol(symbol) or symbol},
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise DataFetchError("Binance REST returned invalid ticker payload")
        return {
            "last": payload.get("lastPrice"),
            "change": payload.get("priceChange"),
            "percentage": payload.get("priceChangePercent"),
            "baseVolume": payload.get("volume"),
            "quoteVolume": payload.get("quoteVolume"),
            "open": payload.get("openPrice"),
            "high": payload.get("highPrice"),
            "low": payload.get("lowPrice"),
            "previousClose": payload.get("prevClosePrice"),
            "timestamp": payload.get("closeTime"),
            "info": payload,
        }

    @staticmethod
    def _fetch_bybit_rest_ticker(symbol: str) -> dict:
        response = requests.get(
            f"{_BYBIT_REST_BASE_URL}/v5/market/tickers",
            params={"category": "spot", "symbol": normalize_crypto_symbol(symbol) or symbol},
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result") if isinstance(payload, dict) else None
        items = result.get("list") if isinstance(result, dict) else None
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            raise DataFetchError("Bybit REST returned invalid ticker payload")
        ticker = items[0]
        change_pct = safe_float(ticker.get("price24hPcnt"))
        return {
            "last": ticker.get("lastPrice"),
            "percentage": change_pct * 100 if change_pct is not None else None,
            "baseVolume": ticker.get("volume24h"),
            "quoteVolume": ticker.get("turnover24h"),
            "open": ticker.get("prevPrice24h"),
            "high": ticker.get("highPrice24h"),
            "low": ticker.get("lowPrice24h"),
            "previousClose": ticker.get("prevPrice24h"),
            "timestamp": payload.get("time"),
            "info": ticker,
        }

    @classmethod
    def _fetch_fallback_kline_rows(
        cls,
        *,
        symbol: str,
        period: str,
        start_ms: int,
        end_ms: int,
        original_error: Exception,
    ) -> tuple[list, str]:
        errors = [f"OKX: {original_error}"]
        for provider, fetcher in (
            ("Binance", cls._fetch_binance_rest_klines),
            ("Bybit", cls._fetch_bybit_rest_klines),
        ):
            try:
                rows = fetcher(symbol=symbol, period=period, start_ms=start_ms, end_ms=end_ms)
                if rows:
                    logger.info("%s REST 兜底获取 %s %s K 线成功", provider, symbol, period)
                    return rows, provider.lower()
                raise DataFetchError("returned empty kline data")
            except Exception as exc:
                errors.append(f"{provider}: {exc}")
        raise DataFetchError("BTC K 线全部数据源失败；" + "; ".join(errors)) from original_error

    @staticmethod
    def _fetch_binance_rest_klines(*, symbol: str, period: str, start_ms: int, end_ms: int) -> list:
        response = requests.get(
            f"{_BINANCE_REST_BASE_URL}/api/v3/klines",
            params={
                "symbol": normalize_crypto_symbol(symbol) or symbol,
                "interval": _BINANCE_INTERVAL_BY_PERIOD[period],
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": 1000,
            },
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise DataFetchError("Binance REST returned invalid kline payload")
        return payload

    @staticmethod
    def _fetch_bybit_rest_klines(*, symbol: str, period: str, start_ms: int, end_ms: int) -> list:
        response = requests.get(
            f"{_BYBIT_REST_BASE_URL}/v5/market/kline",
            params={
                "category": "spot",
                "symbol": normalize_crypto_symbol(symbol) or symbol,
                "interval": _BYBIT_INTERVAL_BY_PERIOD[period],
                "start": start_ms,
                "end": end_ms,
                "limit": 1000,
            },
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result") if isinstance(payload, dict) else None
        rows = result.get("list") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            raise DataFetchError("Bybit REST returned invalid kline payload")
        normalized_rows = [
            [int(row[0]), *row[1:]]
            for row in rows
            if isinstance(row, list) and row
        ]
        return sorted(normalized_rows, key=lambda row: row[0])

    @staticmethod
    def _create_exchange() -> Any:
        return CryptoFetcher._create_public_exchange(_CCXT_EXCHANGE_ID, instrument_type="spot")

    @staticmethod
    def _create_public_exchange(venue: str, *, instrument_type: str) -> Any:
        try:
            import ccxt
        except ImportError as exc:
            raise DataFetchError("ccxt 未安装，请运行 pip install ccxt") from exc

        normalized_venue = str(venue or "").strip().lower()
        exchange_id = "binanceusdm" if normalized_venue == "binance" and instrument_type == "perpetual" else normalized_venue
        exchange_class = getattr(ccxt, exchange_id, None)
        if exchange_class is None:
            raise DataFetchError(f"ccxt 不支持交易所: {exchange_id}")
        options = {"defaultType": "swap" if instrument_type == "perpetual" else "spot"}
        return exchange_class(
            {
                "enableRateLimit": True,
                "timeout": _HTTP_TIMEOUT_SECONDS * 1000,
                "options": options,
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
