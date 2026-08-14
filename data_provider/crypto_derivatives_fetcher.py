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
_OKX_PUBLIC_BASE_URL = "https://www.okx.com/api/v5"
_BYBIT_PUBLIC_BASE_URL = "https://api.bybit.com/v5"
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
        funding_history_payload = self._get_list(
            "/fapi/v1/fundingRate",
            {"symbol": symbol, "limit": 21},
        )
        oi_history_payload = self._get_list(
            "/futures/data/openInterestHist",
            {"symbol": symbol, "period": "1h", "limit": 24},
        )
        long_short_payload = self._get_list(
            "/futures/data/globalLongShortAccountRatio",
            {"symbol": symbol, "period": "1h", "limit": 24},
        )
        order_flow_payload = self._get_array(
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": "5m", "limit": 36},
        )

        funding_rate = _safe_float(funding_payload.get("lastFundingRate"))
        mark_price = _safe_float(funding_payload.get("markPrice"))
        index_price = _safe_float(funding_payload.get("indexPrice"))
        open_interest = _safe_float(oi_payload.get("openInterest"))

        funding_rate_pct = funding_rate * 100 if funding_rate is not None else None
        funding_history = self._funding_history_summary(funding_history_payload)
        oi_history = self._oi_history_summary(oi_history_payload)
        long_short = self._long_short_summary(long_short_payload)
        order_flow = self._order_flow_summary(order_flow_payload, interval_minutes=5)
        if (
            funding_rate is None
            and open_interest is None
            and order_flow.get("data_quality") not in {"available", "partial"}
        ):
            return {
                "provider": "binance_futures",
                "symbol": symbol,
                "data_quality": "unavailable",
                "warnings": ["funding_rate_open_interest_and_order_flow_missing"],
            }
        basis_pct = (
            (mark_price - index_price) / index_price * 100
            if mark_price is not None and index_price is not None and index_price > 0
            else None
        )
        cross_exchange = self._cross_exchange_snapshot(
            binance_rate=funding_rate,
            binance_mark=mark_price,
            binance_index=index_price,
        )
        warnings = []
        if not funding_history_payload:
            warnings.append("funding_history_missing")
        if not oi_history_payload:
            warnings.append("open_interest_history_missing")
        if order_flow.get("data_quality") == "unavailable":
            warnings.append("order_flow_missing")
        elif order_flow.get("data_quality") == "partial":
            warnings.append("order_flow_partial")
        return {
            "provider": "binance_futures",
            "symbol": symbol,
            "data_quality": (
                "available"
                if funding_rate is not None or open_interest is not None
                else "partial"
            ),
            "funding": {
                "rate": funding_rate,
                "rate_pct": round(funding_rate_pct, 4) if funding_rate_pct is not None else None,
                "state": self._funding_state(funding_rate),
                "mark_price": mark_price,
                "index_price": index_price,
                "next_funding_time": _millis_to_iso(funding_payload.get("nextFundingTime")),
                "time": _millis_to_iso(funding_payload.get("time")),
                "history_7d": funding_history,
            },
            "open_interest": {
                "value": open_interest,
                "state": self._open_interest_state(open_interest, mark_price),
                "notional_usdt": round(open_interest * mark_price, 2)
                if open_interest is not None and mark_price is not None
                else None,
                "time": _millis_to_iso(oi_payload.get("time")),
                "history_24h": oi_history,
            },
            "basis": {
                "perpetual_premium_pct": round(basis_pct, 4) if basis_pct is not None else None,
                "state": self._basis_state(basis_pct),
            },
            "long_short_ratio": long_short,
            "order_flow": order_flow,
            "cross_exchange": cross_exchange,
            "leverage_pressure": self._leverage_pressure(funding_rate, open_interest),
            "warnings": warnings,
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

    def _get_list(self, path: str, params: Dict[str, Any]) -> list[Dict[str, Any]]:
        url = f"{self.base_url}{path}"
        try:
            response = requests.get(url, params=params, timeout=self.timeout_seconds)
            response.raise_for_status()
            payload = response.json()
            return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []
        except Exception as exc:
            logger.warning("Binance Futures 衍生品序列获取失败: endpoint=%s error=%s", path, exc)
            return []

    def _get_array(self, path: str, params: Dict[str, Any]) -> list[Any]:
        url = f"{self.base_url}{path}"
        try:
            response = requests.get(url, params=params, timeout=self.timeout_seconds)
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, list) else []
        except Exception as exc:
            logger.warning("Binance Futures 数组数据获取失败: endpoint=%s error=%s", path, exc)
            return []

    def _cross_exchange_snapshot(
        self,
        *,
        binance_rate: Optional[float],
        binance_mark: Optional[float],
        binance_index: Optional[float],
    ) -> Dict[str, Any]:
        venues = [
            self._venue_payload("binance", binance_rate, binance_mark, binance_index),
        ]
        okx = self._external_json(
            f"{_OKX_PUBLIC_BASE_URL}/public/funding-rate",
            {"instId": "BTC-USDT-SWAP"},
        )
        okx_items = okx.get("data") if isinstance(okx, dict) else None
        okx_item = okx_items[0] if isinstance(okx_items, list) and okx_items and isinstance(okx_items[0], dict) else {}
        if okx_item:
            venues.append(
                self._venue_payload(
                    "okx",
                    _safe_float(okx_item.get("fundingRate")),
                    _safe_float(okx_item.get("markPx")),
                    _safe_float(okx_item.get("indexPx")),
                )
            )

        bybit = self._external_json(
            f"{_BYBIT_PUBLIC_BASE_URL}/market/tickers",
            {"category": "linear", "symbol": "BTCUSDT"},
        )
        bybit_result = bybit.get("result") if isinstance(bybit, dict) else None
        bybit_items = bybit_result.get("list") if isinstance(bybit_result, dict) else None
        bybit_item = bybit_items[0] if isinstance(bybit_items, list) and bybit_items and isinstance(bybit_items[0], dict) else {}
        if bybit_item:
            venues.append(
                self._venue_payload(
                    "bybit",
                    _safe_float(bybit_item.get("fundingRate")),
                    _safe_float(bybit_item.get("markPrice")),
                    _safe_float(bybit_item.get("indexPrice")),
                )
            )

        rates = [item["funding_rate"] for item in venues if item.get("funding_rate") is not None]
        spread_pct = (max(rates) - min(rates)) * 100 if len(rates) >= 2 else None
        return {
            "venues": venues,
            "available_venues": len(venues),
            "funding_spread_pct": round(spread_pct, 4) if spread_pct is not None else None,
            "data_quality": "cross_checked" if len(venues) >= 2 else "single_source",
        }

    def _external_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            response = requests.get(url, params=params, timeout=self.timeout_seconds)
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else {}
        except Exception as exc:
            logger.warning("跨交易所衍生品数据获取失败: url=%s error=%s", url, exc)
            return {}

    @staticmethod
    def _venue_payload(
        venue: str,
        funding_rate: Optional[float],
        mark_price: Optional[float],
        index_price: Optional[float],
    ) -> Dict[str, Any]:
        basis_pct = (
            (mark_price - index_price) / index_price * 100
            if mark_price is not None and index_price is not None and index_price > 0
            else None
        )
        return {
            "venue": venue,
            "funding_rate": funding_rate,
            "funding_rate_pct": round(funding_rate * 100, 4) if funding_rate is not None else None,
            "mark_price": mark_price,
            "index_price": index_price,
            "basis_pct": round(basis_pct, 4) if basis_pct is not None else None,
        }

    @staticmethod
    def _funding_history_summary(items: list[Dict[str, Any]]) -> Dict[str, Any]:
        points = [
            (_safe_float(item.get("fundingRate")), _millis_to_iso(item.get("fundingTime")))
            for item in items
        ]
        rates = [rate for rate, _timestamp in points if rate is not None]
        if not rates:
            return {"count": 0, "avg_rate_pct": None, "trend": "missing"}
        split = max(len(rates) // 2, 1)
        early = sum(rates[:split]) / len(rates[:split])
        late = sum(rates[split:]) / len(rates[split:]) if rates[split:] else early
        tolerance = 0.00001
        trend = "rising" if late - early > tolerance else "declining" if early - late > tolerance else "stable"
        return {
            "count": len(rates),
            "avg_rate_pct": round(sum(rates) / len(rates) * 100, 4),
            "min_rate_pct": round(min(rates) * 100, 4),
            "max_rate_pct": round(max(rates) * 100, 4),
            "trend": trend,
            "start_time": next((timestamp for rate, timestamp in points if rate is not None), None),
            "end_time": next((timestamp for rate, timestamp in reversed(points) if rate is not None), None),
        }

    @staticmethod
    def _oi_history_summary(items: list[Dict[str, Any]]) -> Dict[str, Any]:
        values = [
            _safe_float(item.get("sumOpenInterest"))
            for item in items
        ]
        values = [value for value in values if value is not None]
        change_pct = (
            (values[-1] - values[0]) / values[0] * 100
            if len(values) >= 2 and values[0] > 0
            else None
        )
        return {
            "count": len(values),
            "change_pct": round(change_pct, 4) if change_pct is not None else None,
            "state": "expanding" if change_pct is not None and change_pct > 2 else "contracting" if change_pct is not None and change_pct < -2 else "stable" if change_pct is not None else "missing",
        }

    @staticmethod
    def _long_short_summary(items: list[Dict[str, Any]]) -> Dict[str, Any]:
        ratios = [_safe_float(item.get("longShortRatio")) for item in items]
        ratios = [value for value in ratios if value is not None]
        return {
            "current": round(ratios[-1], 4) if ratios else None,
            "change_24h_pct": round((ratios[-1] - ratios[0]) / ratios[0] * 100, 4) if len(ratios) >= 2 and ratios[0] > 0 else None,
            "state": "long_heavy" if ratios and ratios[-1] >= 1.2 else "short_heavy" if ratios and ratios[-1] <= 0.8 else "balanced" if ratios else "missing",
        }

    @staticmethod
    def _order_flow_summary(items: list[Any], *, interval_minutes: int) -> Dict[str, Any]:
        points: list[Dict[str, float]] = []
        for item in items:
            if not isinstance(item, (list, tuple)) or len(item) < 10:
                continue
            open_price = _safe_float(item[1])
            close_price = _safe_float(item[4])
            volume = _safe_float(item[5])
            taker_buy_volume = _safe_float(item[9])
            if (
                open_price is None
                or close_price is None
                or volume is None
                or taker_buy_volume is None
                or volume <= 0
            ):
                continue
            buy_volume = min(max(taker_buy_volume, 0.0), volume)
            sell_volume = max(volume - buy_volume, 0.0)
            points.append(
                {
                    "open": open_price,
                    "close": close_price,
                    "volume": volume,
                    "buy": buy_volume,
                    "sell": sell_volume,
                    "close_time": item[6] if len(item) > 6 else None,
                }
            )

        if not points:
            return {
                "provider": "binance_futures_klines",
                "data_quality": "unavailable",
                "bar_interval": f"{interval_minutes}m",
                "bar_count": 0,
            }

        total_volume = sum(point["volume"] for point in points)
        total_buy = sum(point["buy"] for point in points)
        total_sell = sum(point["sell"] for point in points)
        cvd = total_buy - total_sell
        buy_ratio_pct = total_buy / total_volume * 100 if total_volume > 0 else None
        cvd_pct = cvd / total_volume * 100 if total_volume > 0 else None
        first_open = points[0]["open"]
        last_close = points[-1]["close"]
        price_change_pct = (
            (last_close - first_open) / first_open * 100
            if first_open > 0
            else None
        )

        state = "balanced"
        if buy_ratio_pct is not None and buy_ratio_pct >= 55:
            state = "buy_dominant"
        elif buy_ratio_pct is not None and buy_ratio_pct <= 45:
            state = "sell_dominant"

        divergence = "none"
        if price_change_pct is not None and cvd_pct is not None:
            if price_change_pct >= 0.25 and cvd_pct <= -5:
                divergence = "bearish_price_cvd_divergence"
            elif price_change_pct <= -0.25 and cvd_pct >= 5:
                divergence = "bullish_price_cvd_divergence"
            elif price_change_pct > 0 and cvd_pct > 0:
                divergence = "aligned_buying"
            elif price_change_pct < 0 and cvd_pct < 0:
                divergence = "aligned_selling"

        bar_count = len(points)
        return {
            "provider": "binance_futures_klines",
            "data_quality": "available" if bar_count >= 12 else "partial",
            "bar_interval": f"{interval_minutes}m",
            "bar_count": bar_count,
            "window_minutes": bar_count * interval_minutes,
            "taker_buy_ratio_pct": round(buy_ratio_pct, 2) if buy_ratio_pct is not None else None,
            "taker_buy_base_volume": round(total_buy, 4),
            "taker_sell_base_volume": round(total_sell, 4),
            "cvd_base_volume": round(cvd, 4),
            "cvd_pct_of_volume": round(cvd_pct, 2) if cvd_pct is not None else None,
            "price_change_pct": round(price_change_pct, 2) if price_change_pct is not None else None,
            "state": state,
            "divergence": divergence,
            "as_of": _millis_to_iso(points[-1].get("close_time")),
            "usage": "shadow_execution_confirmation",
        }

    @staticmethod
    def _basis_state(basis_pct: Optional[float]) -> str:
        if basis_pct is None:
            return "missing"
        if basis_pct >= 0.1:
            return "contango"
        if basis_pct <= -0.1:
            return "backwardation"
        return "flat"

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
