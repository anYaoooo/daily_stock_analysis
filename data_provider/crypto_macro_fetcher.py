# -*- coding: utf-8 -*-
"""Structured BTC cross-market correlation context."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_MACRO_SYMBOLS = {
    "nasdaq": "^IXIC",
    "dxy": "DX-Y.NYB",
    "us10y": "^TNX",
    "gold": "GC=F",
}


class CryptoMacroContextFetcher:
    """Build deterministic rolling correlations from closed daily returns."""

    def __init__(self, *, downloader: Optional[Callable[..., pd.DataFrame]] = None) -> None:
        self._downloader = downloader

    def get_btc_macro_context(
        self,
        btc_bars: pd.DataFrame,
        *,
        lookback_days: int = 120,
    ) -> Dict[str, Any]:
        btc_returns = self._btc_returns(btc_bars)
        if btc_returns.empty:
            return {
                "provider": "yfinance",
                "data_quality": "unavailable",
                "warnings": ["btc_daily_returns_missing"],
                "assets": {},
            }

        end_at = datetime.now(timezone.utc).date() + timedelta(days=1)
        start_at = end_at - timedelta(days=max(int(lookback_days), 90) * 2)
        try:
            raw = self._download(
                list(_MACRO_SYMBOLS.values()),
                start=start_at.isoformat(),
                end=end_at.isoformat(),
            )
        except Exception as exc:
            logger.warning("BTC 宏观相关性数据获取失败: %s", exc)
            return {
                "provider": "yfinance",
                "data_quality": "unavailable",
                "warnings": ["macro_download_failed"],
                "assets": {},
            }

        assets: dict[str, Any] = {}
        warnings: list[str] = []
        for name, symbol in _MACRO_SYMBOLS.items():
            close = self._close_series(raw, symbol)
            if close.empty:
                warnings.append(f"{name}_missing")
                assets[name] = self._missing_asset(symbol)
                continue
            macro_returns = close.sort_index().pct_change().dropna().rename("macro_return")
            aligned = pd.concat([btc_returns.rename("btc_return"), macro_returns], axis=1, join="inner").dropna()
            assets[name] = self._correlation_payload(symbol, aligned)

        available_count = sum(1 for item in assets.values() if item.get("sample_count", 0) >= 20)
        return {
            "provider": "yfinance",
            "timezone": "UTC daily close-date alignment",
            "data_quality": "available" if available_count == len(_MACRO_SYMBOLS) else "partial" if available_count else "unavailable",
            "available_assets": available_count,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "assets": assets,
            "warnings": warnings,
        }

    def _download(self, tickers: list[str], *, start: str, end: str) -> pd.DataFrame:
        if self._downloader is not None:
            return self._downloader(tickers=tickers, start=start, end=end)
        import yfinance as yf

        return yf.download(
            tickers=tickers,
            start=start,
            end=end,
            progress=False,
            auto_adjust=True,
            group_by="column",
            timeout=10,
        )

    @staticmethod
    def _btc_returns(bars: pd.DataFrame) -> pd.Series:
        if bars is None or bars.empty or "date" not in bars.columns or "close" not in bars.columns:
            return pd.Series(dtype="float64")
        frame = bars[["date", "close"]].copy()
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce", utc=True).dt.tz_convert(None).dt.normalize()
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        frame = frame.dropna().drop_duplicates("date", keep="last").set_index("date").sort_index()
        return frame["close"].pct_change().dropna()

    @staticmethod
    def _close_series(frame: pd.DataFrame, symbol: str) -> pd.Series:
        if frame is None or frame.empty:
            return pd.Series(dtype="float64")
        series: Any = None
        if isinstance(frame.columns, pd.MultiIndex):
            if ("Close", symbol) in frame.columns:
                series = frame[("Close", symbol)]
            elif (symbol, "Close") in frame.columns:
                series = frame[(symbol, "Close")]
        elif symbol in frame.columns:
            series = frame[symbol]
        elif len(_MACRO_SYMBOLS) == 1 and "Close" in frame.columns:
            series = frame["Close"]
        if series is None:
            return pd.Series(dtype="float64")
        result = pd.to_numeric(series, errors="coerce")
        result.index = pd.to_datetime(result.index, errors="coerce", utc=True).tz_convert(None).normalize()
        return result.dropna().groupby(level=0).last()

    @classmethod
    def _correlation_payload(cls, symbol: str, aligned: pd.DataFrame) -> Dict[str, Any]:
        count = len(aligned)
        corr_30 = cls._correlation(aligned.tail(30))
        corr_60 = cls._correlation(aligned.tail(60))
        current = corr_30 if corr_30 is not None else corr_60
        return {
            "symbol": symbol,
            "sample_count": count,
            "correlation_30d": round(corr_30, 4) if corr_30 is not None else None,
            "correlation_60d": round(corr_60, 4) if corr_60 is not None else None,
            "state": cls._correlation_state(current, count),
            "range_start": aligned.index.min().isoformat() if count else None,
            "range_end": aligned.index.max().isoformat() if count else None,
        }

    @staticmethod
    def _correlation(frame: pd.DataFrame) -> Optional[float]:
        if len(frame) < 20:
            return None
        value = frame["btc_return"].corr(frame["macro_return"])
        return float(value) if pd.notna(value) else None

    @staticmethod
    def _correlation_state(value: Optional[float], sample_count: int) -> str:
        if value is None or sample_count < 20:
            return "insufficient"
        strength = "high" if abs(value) >= 0.6 else "moderate" if abs(value) >= 0.3 else "low"
        direction = "positive" if value > 0 else "negative" if value < 0 else "flat"
        return f"{strength}_{direction}"

    @staticmethod
    def _missing_asset(symbol: str) -> Dict[str, Any]:
        return {
            "symbol": symbol,
            "sample_count": 0,
            "correlation_30d": None,
            "correlation_60d": None,
            "state": "missing",
        }
