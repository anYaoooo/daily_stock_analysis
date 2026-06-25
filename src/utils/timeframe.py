# -*- coding: utf-8 -*-
"""Helpers for report/backtest timeframe labels."""

from __future__ import annotations

from typing import Any


def normalize_analysis_mode(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"hourly", "hour", "1h", "intraday"}:
        return "hourly"
    return "daily"


def analysis_timeframe_label(mode: Any, report_language: str = "zh") -> str:
    normalized = normalize_analysis_mode(mode)
    language = str(report_language or "zh").strip().lower()
    if language == "en":
        return "Hourly" if normalized == "hourly" else "Daily"
    return "小时线" if normalized == "hourly" else "日线"


def horizon_to_analysis_mode(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"intraday", "hourly", "hour", "1h"}:
        return "hourly"
    return "daily"
