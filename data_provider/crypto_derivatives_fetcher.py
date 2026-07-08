# -*- coding: utf-8 -*-
"""Public BTC derivatives data fetcher."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests

from .crypto_fetcher import normalize_crypto_symbol

logger = logging.getLogger(__name__)

_BINANCE_FUTURES_BASE_URL = "https://fapi.binance.com"
_HTTP_TIMEOUT_SECONDS = 10


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _millis_to_iso(value: Any) -> Optional[str]:
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).isoformat()


class CryptoDerivativesFetcher:
    """Fetch low-sensitivity public derivatives context for BTC analysis."""

    def __init__(
        self,
        *,
        base_url: str = _BINANCE_FUTURES_BASE_URL,
        timeout_seconds: int = _HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = max(1, int(timeout_seconds or _HTTP_TIMEOUT_SECONDS))

    def get_btc_derivatives_context(self, code: str) -> Optional[Dict[str, Any]]:
        symbol = normalize_crypto_symbol(code)
        if symbol != "BTCUSDT":
            return None

        funding_payload = self._get_json("/fapi/v1/premiumIndex", {"symbol": symbol})
        oi_payload = self._get_json("/fapi/v1/openInterest", {"symbol": symbol})

        funding_rate = _safe_float(funding_payload.get("lastFundingRate"))
        mark_price = _safe_float(funding_payload.get("markPrice"))
        index_price = _safe_float(funding_payload.get("indexPrice"))
        open_interest = _safe_float(oi_payload.get("openInterest"))

        if funding_rate is None and open_interest is None:
            return {
                "provider": "binance_futures",
                "symbol": symbol,
                "data_quality": "unavailable",
                "warnings": ["funding_rate_and_open_interest_missing"],
            }

        funding_rate_pct = funding_rate * 100 if funding_rate is not None else None
        return {
            "provider": "binance_futures",
            "symbol": symbol,
            "data_quality": "available",
            "funding": {
                "rate": funding_rate,
                "rate_pct": round(funding_rate_pct, 4) if funding_rate_pct is not None else None,
                "state": self._funding_state(funding_rate),
                "mark_price": mark_price,
                "index_price": index_price,
                "next_funding_time": _millis_to_iso(funding_payload.get("nextFundingTime")),
                "time": _millis_to_iso(funding_payload.get("time")),
            },
            "open_interest": {
                "value": open_interest,
                "state": self._open_interest_state(open_interest, mark_price),
                "notional_usdt": round(open_interest * mark_price, 2)
                if open_interest is not None and mark_price is not None
                else None,
                "time": _millis_to_iso(oi_payload.get("time")),
            },
            "leverage_pressure": self._leverage_pressure(funding_rate, open_interest),
            "warnings": [],
        }

    def _get_json(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            response = requests.get(url, params=params, timeout=self.timeout_seconds)
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else {}
        except Exception as exc:
            logger.warning("Binance Futures 衍生品数据获取失败: endpoint=%s error=%s", path, exc)
            return {}

    @staticmethod
    def _funding_state(rate: Optional[float]) -> str:
        if rate is None:
            return "missing"
        if rate >= 0.0005:
            return "positive_crowded"
        if rate >= 0.0001:
            return "positive"
        if rate <= -0.0005:
            return "negative_crowded"
        if rate <= -0.0001:
            return "negative"
        return "neutral"

    @staticmethod
    def _open_interest_state(open_interest: Optional[float], mark_price: Optional[float]) -> str:
        if open_interest is None:
            return "missing"
        if mark_price is not None and open_interest * mark_price >= 10_000_000_000:
            return "high_notional"
        return "available"

    @classmethod
    def _leverage_pressure(cls, rate: Optional[float], open_interest: Optional[float]) -> str:
        funding_state = cls._funding_state(rate)
        if open_interest is None:
            return "funding_only"
        if funding_state == "positive_crowded":
            return "long_crowding_risk"
        if funding_state == "negative_crowded":
            return "short_crowding_squeeze_risk"
        if funding_state in {"positive", "negative"}:
            return f"{funding_state}_leverage_bias"
        return "neutral"
