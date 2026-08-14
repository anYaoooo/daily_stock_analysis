"""Local-first BTC OHLCV access for deterministic analysis and backtests."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import pandas as pd

from data_provider.crypto_fetcher import CryptoFetcher, normalize_crypto_symbol
from src.storage import CryptoOhlcvBar, DatabaseManager

logger = logging.getLogger(__name__)

_SUPPORTED_PERIODS = {"daily": timedelta(days=1), "hourly": timedelta(hours=1)}


class CryptoMarketDataService:
    """Serve complete closed bars from SQLite and refresh missing coverage remotely."""

    def __init__(
        self,
        *,
        db_manager: Optional[DatabaseManager] = None,
        fetcher: Optional[CryptoFetcher] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.db = db_manager or DatabaseManager.get_instance()
        self.fetcher = fetcher or CryptoFetcher()
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    def get_bars(
        self,
        code: str,
        *,
        period: str,
        days: int,
        instrument: Optional[dict[str, Any]] = None,
    ) -> pd.DataFrame:
        """Return closed bars, using remote data only when local coverage is incomplete."""
        if period not in _SUPPORTED_PERIODS:
            raise ValueError(f"unsupported local crypto cache period: {period}")
        if days < 1:
            raise ValueError("days must be at least 1")

        identity = self._identity(code, instrument)
        fetched_at = self._utc_naive(self._now_provider())
        start_at, latest_closed_at = self._requested_window(fetched_at, period=period, days=days)
        local_rows = self.db.get_crypto_ohlcv_bars(
            **identity,
            period=period,
            start_at=start_at,
            end_at=latest_closed_at,
        )
        if self._covers_window(local_rows, start_at=start_at, end_at=latest_closed_at, period=period):
            return self._rows_to_dataframe(local_rows, identity=identity, period=period, cache_hit=True)

        fresh = self._fetch_remote(identity, period=period, days=days, instrument=instrument)
        closed = self._closed_dataframe(fresh, period=period, fetched_at=fetched_at)
        if closed.empty:
            raise ValueError(f"no closed {period} BTC bars returned by remote source")

        try:
            self._persist(closed, identity=identity, period=period, fetched_at=fetched_at)
            local_rows = self.db.get_crypto_ohlcv_bars(
                **identity,
                period=period,
                start_at=start_at,
                end_at=latest_closed_at,
            )
            if self._covers_window(local_rows, start_at=start_at, end_at=latest_closed_at, period=period):
                return self._rows_to_dataframe(local_rows, identity=identity, period=period, cache_hit=False)
        except Exception as exc:
            logger.warning("BTC 本地行情缓存写入失败，使用本次远端数据: %s", exc)

        return self._filter_window(closed, start_at=start_at, end_at=latest_closed_at, period=period)

    @staticmethod
    def _utc_naive(value: datetime) -> datetime:
        if value.tzinfo is not None and value.utcoffset() is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    @staticmethod
    def _identity(code: str, instrument: Optional[dict[str, Any]]) -> dict[str, str]:
        normalized_code = normalize_crypto_symbol(code) or str(code).strip().upper()
        contract = instrument or {}
        instrument_type = str(contract.get("type") or contract.get("instrument_type") or "spot").lower()
        venue = str(contract.get("venue") or "okx").lower()
        return {
            "code": normalized_code,
            "venue": venue,
            "instrument_type": instrument_type,
            "price_type": "trade_and_mark" if instrument_type == "perpetual" else "trade",
        }

    @staticmethod
    def _requested_window(now: datetime, *, period: str, days: int) -> tuple[datetime, datetime]:
        duration = _SUPPORTED_PERIODS[period]
        if period == "daily":
            current_open = now.replace(hour=0, minute=0, second=0, microsecond=0)
            bar_count = days
        else:
            current_open = now.replace(minute=0, second=0, microsecond=0)
            # `days` is a calendar-day lookback for both daily and hourly data.
            # The hourly series therefore needs 24 bars per requested day.
            bar_count = days * 24
        latest_closed = current_open - duration
        start_at = latest_closed - duration * (bar_count - 1)
        return start_at, latest_closed

    def _fetch_remote(
        self,
        identity: dict[str, str],
        *,
        period: str,
        days: int,
        instrument: Optional[dict[str, Any]],
    ) -> pd.DataFrame:
        if identity["instrument_type"] == "perpetual":
            contract = instrument or {}
            return self.fetcher.get_perpetual_kline_data(
                identity["code"],
                period=period,
                days=days,
                venue=identity["venue"],
                margin_mode=str(contract.get("margin_mode") or "isolated"),
            )
        return self.fetcher.get_kline_data(identity["code"], period=period, days=days)

    def _closed_dataframe(self, frame: pd.DataFrame, *, period: str, fetched_at: datetime) -> pd.DataFrame:
        if frame is None or frame.empty:
            return pd.DataFrame()
        output = frame.copy()
        output["_open_time"] = pd.to_datetime(output["date"], utc=True, errors="coerce").dt.tz_convert(None)
        output = output.dropna(subset=["_open_time", "open", "high", "low", "close"])
        duration = _SUPPORTED_PERIODS[period]
        output = output[output["_open_time"] + duration <= fetched_at].copy()
        output = output.drop_duplicates(subset=["_open_time"], keep="last").sort_values("_open_time")
        output.attrs.update(frame.attrs)
        return output.reset_index(drop=True)

    def _persist(self, frame: pd.DataFrame, *, identity: dict[str, str], period: str, fetched_at: datetime) -> None:
        records = []
        source = str(frame.attrs.get("source") or "CryptoFetcher")
        for row in frame.to_dict(orient="records"):
            records.append(
                {
                    **identity,
                    "period": period,
                    "open_time": self._as_datetime(row["_open_time"]),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": self._number(row.get("volume")),
                    "amount": self._number(row.get("amount")),
                    "execution_open": self._number(row.get("execution_open")),
                    "execution_high": self._number(row.get("execution_high")),
                    "execution_low": self._number(row.get("execution_low")),
                    "execution_close": self._number(row.get("execution_close")),
                    "mark_open": self._number(row.get("mark_open")),
                    "mark_high": self._number(row.get("mark_high")),
                    "mark_low": self._number(row.get("mark_low")),
                    "mark_close": self._number(row.get("mark_close")),
                    "funding_rates": self._json_value(row.get("funding_rates")),
                    "source": source,
                    "fetched_at": fetched_at,
                }
            )
        content_hash = self._content_hash(records)
        self.db.upsert_crypto_ohlcv_bars(
            records,
            sync_state={
                **identity,
                "period": period,
                "latest_closed_at": records[-1]["open_time"],
                "content_hash": content_hash,
                "source": source,
                "synced_at": fetched_at,
            },
        )

    @staticmethod
    def _as_datetime(value: Any) -> datetime:
        if isinstance(value, pd.Timestamp):
            return value.to_pydatetime().replace(tzinfo=None)
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        return pd.Timestamp(value).to_pydatetime().replace(tzinfo=None)

    @staticmethod
    def _number(value: Any) -> Optional[float]:
        if value is None or pd.isna(value):
            return None
        return float(value)

    @staticmethod
    def _json_value(value: Any) -> Optional[str]:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return None
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _content_hash(records: list[dict[str, Any]]) -> str:
        digest = hashlib.sha256()
        for record in records:
            digest.update(
                json.dumps(record, ensure_ascii=True, default=str, sort_keys=True, separators=(",", ":")).encode("utf-8")
            )
        return f"sha256:{digest.hexdigest()}"

    @staticmethod
    def _covers_window(rows: list[CryptoOhlcvBar], *, start_at: datetime, end_at: datetime, period: str) -> bool:
        duration = _SUPPORTED_PERIODS[period]
        expected_count = int((end_at - start_at) / duration) + 1
        if len(rows) != expected_count:
            return False
        expected = start_at
        for row in rows:
            if row.open_time != expected:
                return False
            expected += duration
        return True

    def _rows_to_dataframe(
        self,
        rows: list[CryptoOhlcvBar],
        *,
        identity: dict[str, str],
        period: str,
        cache_hit: bool,
    ) -> pd.DataFrame:
        payload = []
        for row in rows:
            item = {
                "date": row.open_time.strftime("%Y-%m-%d") if period == "daily" else row.open_time.strftime("%Y-%m-%d %H:%M"),
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
                "amount": row.amount,
                "execution_open": row.execution_open,
                "execution_high": row.execution_high,
                "execution_low": row.execution_low,
                "execution_close": row.execution_close,
                "mark_open": row.mark_open,
                "mark_high": row.mark_high,
                "mark_low": row.mark_low,
                "mark_close": row.mark_close,
                "funding_rates": json.loads(row.funding_rates) if row.funding_rates else (),
            }
            payload.append(item)
        result = pd.DataFrame(payload)
        result.attrs.update({
            "source": "local_cache",
            "cached_source": rows[-1].source if rows else None,
            "venue": identity["venue"],
            "instrument_type": identity["instrument_type"],
            "price_type": identity["price_type"],
            "canonical_symbol": "BTC-USDT-PERP" if identity["instrument_type"] == "perpetual" else "BTC-USDT",
            "fetched_at": rows[-1].fetched_at.replace(tzinfo=timezone.utc).isoformat() if rows else None,
            "cache_hit": cache_hit,
        })
        return result

    def _filter_window(self, frame: pd.DataFrame, *, start_at: datetime, end_at: datetime, period: str) -> pd.DataFrame:
        result = frame[(frame["_open_time"] >= start_at) & (frame["_open_time"] <= end_at)].copy()
        result["date"] = result["_open_time"].dt.strftime("%Y-%m-%d" if period == "daily" else "%Y-%m-%d %H:%M")
        result = result.drop(columns=["_open_time"])
        return result.reset_index(drop=True)
