# -*- coding: utf-8 -*-
"""Canonical BTC spot/perpetual instrument contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping, Optional


SUPPORTED_CRYPTO_VENUES = {"okx", "bybit", "binance"}
SUPPORTED_INSTRUMENT_TYPES = {"spot", "perpetual"}
SUPPORTED_PRICE_TYPES = {"trade", "mark", "index"}


@dataclass(frozen=True)
class CryptoInstrument:
    """Normalized BTC/USDT market identity used across analysis and execution."""

    instrument_type: str
    venue: str
    canonical_symbol: str
    market_symbol: str
    trigger_price_type: str = "trade"
    fill_price_type: str = "trade"
    liquidation_price_type: Optional[str] = None
    margin_mode: Optional[str] = None

    def to_contract(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["type"] = payload.pop("instrument_type")
        payload["symbol"] = payload.pop("canonical_symbol")
        return payload


def resolve_crypto_instrument(
    value: Any,
    *,
    default_type: str = "spot",
    venue: str = "okx",
    margin_mode: Optional[str] = None,
) -> Optional[CryptoInstrument]:
    """Resolve a BTC alias or instrument mapping without collapsing spot/perpetual."""

    normalized_default_type = str(default_type or "spot").strip().lower()
    if normalized_default_type == "swap":
        normalized_default_type = "perpetual"
    if normalized_default_type not in SUPPORTED_INSTRUMENT_TYPES:
        return None

    raw_mapping = value if isinstance(value, Mapping) else {}
    raw_symbol = (
        raw_mapping.get("market_symbol")
        or raw_mapping.get("symbol")
        or raw_mapping.get("canonical_symbol")
        or "BTC"
        if raw_mapping
        else value
    )
    raw_type = str(raw_mapping.get("type") or raw_mapping.get("instrument_type") or "").strip().lower()
    if raw_type == "swap":
        raw_type = "perpetual"

    text = str(raw_symbol or "").strip().upper()
    compact = text
    for token in ("-", "_", "/", ":"):
        compact = compact.replace(token, "")

    explicit_perpetual = text.endswith("-PERP") or text.endswith(":USDT") or compact.endswith("PERP")
    explicit_spot = text in {"BTC-USDT", "BTC/USDT", "BTC_USDT", "BTC-USD", "BTC/USD"}
    if compact.endswith("PERP"):
        compact = compact[:-4]
    if compact not in {"BTC", "BTCUSD", "BTCUSDT", "BTCUSDTUSDT"}:
        return None

    instrument_type = raw_type or (
        "perpetual" if explicit_perpetual else "spot" if explicit_spot else normalized_default_type
    )
    if instrument_type not in SUPPORTED_INSTRUMENT_TYPES:
        return None

    normalized_venue = str(raw_mapping.get("venue") or venue or "okx").strip().lower()
    if normalized_venue not in SUPPORTED_CRYPTO_VENUES:
        return None

    normalized_margin = str(raw_mapping.get("margin_mode") or margin_mode or "").strip().lower() or None
    if instrument_type == "spot":
        normalized_margin = None
    elif normalized_margin not in {"cross", "isolated"}:
        normalized_margin = "isolated"

    trigger_price_type = str(raw_mapping.get("trigger_price_type") or "trade").strip().lower()
    fill_price_type = str(raw_mapping.get("fill_price_type") or "trade").strip().lower()
    liquidation_price_type = str(raw_mapping.get("liquidation_price_type") or "").strip().lower() or None
    if instrument_type == "perpetual" and liquidation_price_type is None:
        liquidation_price_type = "mark"

    if trigger_price_type not in SUPPORTED_PRICE_TYPES or fill_price_type != "trade":
        return None
    if instrument_type == "perpetual" and liquidation_price_type != "mark":
        return None
    if instrument_type == "spot" and liquidation_price_type is not None:
        return None

    return CryptoInstrument(
        instrument_type=instrument_type,
        venue=normalized_venue,
        canonical_symbol="BTC-USDT-PERP" if instrument_type == "perpetual" else "BTC-USDT",
        market_symbol="BTC/USDT:USDT" if instrument_type == "perpetual" else "BTC/USDT",
        trigger_price_type=trigger_price_type,
        fill_price_type=fill_price_type,
        liquidation_price_type=liquidation_price_type,
        margin_mode=normalized_margin,
    )


def instrument_type_from_market_symbol(symbol: str) -> str:
    """Return the canonical instrument type for a validated market symbol."""

    return "perpetual" if str(symbol or "").strip().upper().endswith(":USDT") else "spot"
