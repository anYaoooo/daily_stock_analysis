"""Local-first BTC OHLCV access for deterministic analysis and backtests."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
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

    def get_cached_contiguous_bars(
        self,
        code: str,
        *,
        period: str,
        days: int,
        instrument: Optional[dict[str, Any]] = None,
    ) -> pd.DataFrame:
        """Return the latest contiguous cached suffix without remote refresh.

        Optional consumers such as shadow models can use a long local history
        without turning an incomplete training window into a failure of the
        short-window trading context.
        """
        if period not in _SUPPORTED_PERIODS:
            raise ValueError(f"unsupported local crypto cache period: {period}")
        if days < 1:
            raise ValueError("days must be at least 1")

        identity = self._identity(code, instrument)
        fetched_at = self._utc_naive(self._now_provider())
        start_at, latest_closed_at = self._requested_window(fetched_at, period=period, days=days)
        rows = self.db.get_crypto_ohlcv_bars(
            **identity,
            period=period,
            start_at=start_at,
            end_at=latest_closed_at,
        )
        contiguous_rows = self._latest_contiguous_suffix(
            rows,
            end_at=latest_closed_at,
            period=period,
        )
        result = self._rows_to_dataframe(
            contiguous_rows,
            identity=identity,
            period=period,
            cache_hit=True,
        )
        result.attrs["requested_window_complete"] = self._covers_window(
            contiguous_rows,
            start_at=start_at,
            end_at=latest_closed_at,
            period=period,
        )
        return result

    def backfill_perpetual_history(
        self,
        code: str = "BTC",
        *,
        start_at: datetime,
        end_at: Optional[datetime] = None,
        period: str = "hourly",
        venue: str = "okx",
        margin_mode: str = "isolated",
        chunk_days: int = 30,
        force: bool = False,
        progress_callback: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> dict[str, Any]:
        """Backfill aligned closed perpetual candles in resumable chunks."""
        if period not in _SUPPORTED_PERIODS:
            raise ValueError(f"unsupported local crypto cache period: {period}")
        if chunk_days < 1:
            raise ValueError("chunk_days must be at least 1")

        duration = _SUPPORTED_PERIODS[period]
        normalized_start = self._aligned_open_time(start_at, period=period, field_name="start_at")
        latest_closed = self._latest_closed_open(self._utc_naive(self._now_provider()), period=period)
        normalized_end = (
            latest_closed
            if end_at is None
            else min(self._aligned_open_time(end_at, period=period, field_name="end_at"), latest_closed)
        )
        if normalized_end < normalized_start:
            raise ValueError("backfill range contains no closed bars")

        identity = self._identity(
            code,
            {"type": "perpetual", "venue": venue, "margin_mode": margin_mode},
        )
        bars_per_chunk = max(int(timedelta(days=chunk_days) / duration), 1)
        expected_count = int((normalized_end - normalized_start) / duration) + 1
        chunk_total = (expected_count + bars_per_chunk - 1) // bars_per_chunk
        fetched_at = self._utc_naive(self._now_provider())
        fetched_chunks = 0
        skipped_chunks = 0
        written_bars = 0
        cursor = normalized_start

        for chunk_index in range(1, chunk_total + 1):
            chunk_end = min(cursor + duration * (bars_per_chunk - 1), normalized_end)
            local_rows = self.db.get_crypto_ohlcv_bars(
                **identity,
                period=period,
                start_at=cursor,
                end_at=chunk_end,
            )
            if not force and self._covers_window(
                local_rows,
                start_at=cursor,
                end_at=chunk_end,
                period=period,
            ) and self._perpetual_rows_complete(local_rows):
                skipped_chunks += 1
                event = {
                    "index": chunk_index,
                    "total": chunk_total,
                    "status": "skipped",
                    "start_at": cursor,
                    "end_at": chunk_end,
                    "bar_count": len(local_rows),
                }
            else:
                fresh = self.fetcher.get_perpetual_kline_range(
                    identity["code"],
                    start_at=cursor,
                    end_at=chunk_end,
                    period=period,
                    venue=identity["venue"],
                    margin_mode=margin_mode,
                    include_funding=False,
                    require_funding=False,
                )
                closed = self._closed_dataframe(fresh, period=period, fetched_at=fetched_at)
                closed = closed[
                    (closed["_open_time"] >= cursor) & (closed["_open_time"] <= chunk_end)
                ].copy()
                if not self._dataframe_covers_window(
                    closed,
                    start_at=cursor,
                    end_at=chunk_end,
                    period=period,
                ):
                    expected = int((chunk_end - cursor) / duration) + 1
                    raise ValueError(
                        "OKX returned incomplete aligned BTC history for "
                        f"{cursor.isoformat()} ~ {chunk_end.isoformat()}: "
                        f"expected={expected}, actual={len(closed)}"
                    )
                written = self._persist(closed, identity=identity, period=period, fetched_at=fetched_at)
                fetched_chunks += 1
                written_bars += written
                event = {
                    "index": chunk_index,
                    "total": chunk_total,
                    "status": "fetched",
                    "start_at": cursor,
                    "end_at": chunk_end,
                    "bar_count": len(closed),
                }
            if progress_callback is not None:
                progress_callback(event)
            cursor = chunk_end + duration

        summary = self.get_history_coverage(
            code,
            start_at=normalized_start,
            end_at=normalized_end,
            period=period,
            instrument_type="perpetual",
            venue=venue,
        )
        summary.update(
            {
                "chunk_days": chunk_days,
                "chunk_total": chunk_total,
                "fetched_chunks": fetched_chunks,
                "skipped_chunks": skipped_chunks,
                "written_bars": written_bars,
            }
        )
        return summary

    def get_history_coverage(
        self,
        code: str,
        *,
        start_at: datetime,
        end_at: datetime,
        period: str,
        instrument_type: str = "perpetual",
        venue: str = "okx",
    ) -> dict[str, Any]:
        """Return deterministic continuity and field-completeness statistics."""
        if period not in _SUPPORTED_PERIODS:
            raise ValueError(f"unsupported local crypto cache period: {period}")
        normalized_start = self._aligned_open_time(start_at, period=period, field_name="start_at")
        normalized_end = self._aligned_open_time(end_at, period=period, field_name="end_at")
        if normalized_end < normalized_start:
            raise ValueError("end_at must not be earlier than start_at")
        identity = self._identity(code, {"type": instrument_type, "venue": venue})
        rows = self.db.get_crypto_ohlcv_bars(
            **identity,
            period=period,
            start_at=normalized_start,
            end_at=normalized_end,
        )
        duration = _SUPPORTED_PERIODS[period]
        expected_times = {
            normalized_start + duration * index
            for index in range(int((normalized_end - normalized_start) / duration) + 1)
        }
        actual_times = {row.open_time for row in rows}
        missing_times = sorted(expected_times - actual_times)
        return {
            "code": identity["code"],
            "venue": identity["venue"],
            "instrument_type": identity["instrument_type"],
            "price_type": identity["price_type"],
            "period": period,
            "range_start": normalized_start,
            "range_end": normalized_end,
            "coverage_start": rows[0].open_time if rows else None,
            "coverage_end": rows[-1].open_time if rows else None,
            "expected_bars": len(expected_times),
            "actual_bars": len(rows),
            "missing_bars": len(missing_times),
            "first_missing_at": missing_times[0] if missing_times else None,
            "execution_missing_bars": sum(row.execution_close is None for row in rows),
            "mark_missing_bars": sum(row.mark_close is None for row in rows),
            "funding_complete_bars": sum(bool(row.funding_complete) for row in rows),
        }

    def export_history_csv(
        self,
        output_path: Path,
        *,
        code: str,
        start_at: datetime,
        end_at: datetime,
        period: str,
        instrument_type: str = "perpetual",
        venue: str = "okx",
    ) -> Path:
        """Export one cached crypto series to a model-friendly CSV file."""
        identity = self._identity(code, {"type": instrument_type, "venue": venue})
        rows = self.db.get_crypto_ohlcv_bars(
            **identity,
            period=period,
            start_at=self._utc_naive(start_at),
            end_at=self._utc_naive(end_at),
        )
        frame = self._rows_to_dataframe(rows, identity=identity, period=period, cache_hit=True)
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(destination, index=False, encoding="utf-8")
        return destination.resolve()

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

    @staticmethod
    def _latest_closed_open(now: datetime, *, period: str) -> datetime:
        duration = _SUPPORTED_PERIODS[period]
        if period == "daily":
            current_open = now.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            current_open = now.replace(minute=0, second=0, microsecond=0)
        return current_open - duration

    @classmethod
    def _aligned_open_time(cls, value: datetime, *, period: str, field_name: str) -> datetime:
        normalized = cls._utc_naive(value)
        if period == "daily":
            aligned = normalized.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            aligned = normalized.replace(minute=0, second=0, microsecond=0)
        if normalized != aligned:
            raise ValueError(f"{field_name} must be aligned to a {period} UTC bar open")
        return aligned

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

    def _persist(self, frame: pd.DataFrame, *, identity: dict[str, str], period: str, fetched_at: datetime) -> int:
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
                    "funding_complete": self._boolean(
                        row.get("funding_complete"),
                        default=bool(frame.attrs.get("funding_complete", False)),
                    ),
                    "source": source,
                    "fetched_at": fetched_at,
                }
            )
        content_hash = self._content_hash(records)
        return self.db.upsert_crypto_ohlcv_bars(
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
    def _boolean(value: Any, *, default: bool = False) -> bool:
        if value is None:
            return default
        try:
            if pd.isna(value):
                return default
        except (TypeError, ValueError):
            pass
        return bool(value)

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

    @staticmethod
    def _latest_contiguous_suffix(
        rows: list[CryptoOhlcvBar],
        *,
        end_at: datetime,
        period: str,
    ) -> list[CryptoOhlcvBar]:
        if not rows or rows[-1].open_time != end_at:
            return []
        duration = _SUPPORTED_PERIODS[period]
        start_index = len(rows) - 1
        expected = end_at
        while start_index >= 0 and rows[start_index].open_time == expected:
            start_index -= 1
            expected -= duration
        return rows[start_index + 1:]

    @staticmethod
    def _dataframe_covers_window(
        frame: pd.DataFrame,
        *,
        start_at: datetime,
        end_at: datetime,
        period: str,
    ) -> bool:
        if frame is None or frame.empty or "_open_time" not in frame:
            return False
        duration = _SUPPORTED_PERIODS[period]
        expected_count = int((end_at - start_at) / duration) + 1
        open_times = list(frame["_open_time"])
        if len(open_times) != expected_count:
            return False
        if not all(timestamp == start_at + duration * index for index, timestamp in enumerate(open_times)):
            return False
        required_columns = (
            "execution_open",
            "execution_high",
            "execution_low",
            "execution_close",
            "mark_open",
            "mark_high",
            "mark_low",
            "mark_close",
        )
        return all(column in frame and frame[column].notna().all() for column in required_columns)

    @staticmethod
    def _perpetual_rows_complete(rows: list[CryptoOhlcvBar]) -> bool:
        return all(
            value is not None
            for row in rows
            for value in (
                row.execution_open,
                row.execution_high,
                row.execution_low,
                row.execution_close,
                row.mark_open,
                row.mark_high,
                row.mark_low,
                row.mark_close,
            )
        )

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
                "funding_complete": bool(row.funding_complete),
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
            "funding_complete": all(bool(row.funding_complete) for row in rows) if rows else False,
            "cache_hit": cache_hit,
        })
        return result

    def _filter_window(self, frame: pd.DataFrame, *, start_at: datetime, end_at: datetime, period: str) -> pd.DataFrame:
        result = frame[(frame["_open_time"] >= start_at) & (frame["_open_time"] <= end_at)].copy()
        result["date"] = result["_open_time"].dt.strftime("%Y-%m-%d" if period == "daily" else "%Y-%m-%d %H:%M")
        result = result.drop(columns=["_open_time"])
        return result.reset_index(drop=True)
