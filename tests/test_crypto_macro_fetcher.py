# -*- coding: utf-8 -*-
"""Tests for BTC cross-market correlation context."""

from __future__ import annotations

import pandas as pd

from data_provider.crypto_macro_fetcher import CryptoMacroContextFetcher


def _prices(returns: list[float], start: float = 100.0) -> list[float]:
    values = [start]
    for item in returns:
        values.append(values[-1] * (1 + item))
    return values


def test_crypto_macro_context_aligns_close_dates_and_computes_correlations() -> None:
    dates = pd.bdate_range("2026-01-01", periods=80)
    returns = [0.01 if index % 2 == 0 else -0.006 for index in range(79)]
    btc = pd.DataFrame({"date": dates, "close": _prices(returns, 60000.0)})
    columns = pd.MultiIndex.from_tuples(
        [
            ("Close", "^IXIC"),
            ("Close", "DX-Y.NYB"),
            ("Close", "^TNX"),
            ("Close", "GC=F"),
        ]
    )
    macro = pd.DataFrame(
        {
            columns[0]: _prices(returns, 20000.0),
            columns[1]: _prices([-item for item in returns], 100.0),
            columns[2]: _prices([item * 0.4 for item in returns], 4.0),
            columns[3]: _prices([item * 0.2 for item in returns], 2000.0),
        },
        index=dates,
    )
    macro.columns = columns
    fetcher = CryptoMacroContextFetcher(downloader=lambda **_kwargs: macro)

    context = fetcher.get_btc_macro_context(btc)

    assert context["data_quality"] == "available"
    assert context["available_assets"] == 4
    assert context["assets"]["nasdaq"]["correlation_30d"] > 0.99
    assert context["assets"]["nasdaq"]["state"] == "high_positive"
    assert context["assets"]["dxy"]["correlation_30d"] < -0.99
    assert context["assets"]["dxy"]["state"] == "high_negative"
    assert context["timezone"] == "UTC daily close-date alignment"


def test_crypto_macro_context_fails_open_when_download_fails() -> None:
    btc = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=30),
            "close": range(100, 130),
        }
    )

    def fail(**_kwargs):
        raise RuntimeError("network unavailable")

    context = CryptoMacroContextFetcher(downloader=fail).get_btc_macro_context(btc)

    assert context["data_quality"] == "unavailable"
    assert context["warnings"] == ["macro_download_failed"]
