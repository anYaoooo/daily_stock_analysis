# -*- coding: utf-8 -*-
"""BTC-only runtime helpers."""

from __future__ import annotations

from typing import Iterable, List


BTC_CANONICAL_CODE = "BTC"
BTC_SUPPORTED_ALIASES = {"BTC", "BTCUSDT", "BTC-USD", "BTC/USD", "BTCUSD"}


def is_supported_btc_code(code: str) -> bool:
    """Return whether a user supplied symbol is an accepted BTC alias."""
    return (code or "").strip().upper() in BTC_SUPPORTED_ALIASES


def canonical_btc_code(code: str) -> str:
    """Normalize any accepted BTC alias to the canonical runtime code."""
    raw = (code or "").strip().upper()
    if not is_supported_btc_code(raw):
        raise ValueError(f"仅支持 BTC 交易分析，当前输入不支持: {code}")
    return BTC_CANONICAL_CODE


def normalize_btc_code_list(codes: Iterable[str]) -> List[str]:
    """Normalize a code iterable to a de-duplicated BTC-only list."""
    normalized: List[str] = []
    seen = set()
    for code in codes:
        if not (code or "").strip():
            continue
        canonical = canonical_btc_code(code)
        if canonical in seen:
            continue
        seen.add(canonical)
        normalized.append(canonical)
    return normalized
