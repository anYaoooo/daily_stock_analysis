# -*- coding: utf-8 -*-
"""Causal feature and multi-task label construction for BTC sequence models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

import numpy as np
import pandas as pd


DEFAULT_TRANSFORMER_HORIZONS = {"15m": 3, "1h": 12, "4h": 48}
REGIME_LABELS = ("trend_up", "trend_down", "high_volatility", "sideways")


@dataclass(frozen=True)
class TransformerFeatureConfig:
    """Configuration shared by feature generation and sequence datasets.

    Horizon values are measured in bars, so 5-minute data can use the defaults
    while hourly data should explicitly pass ``{"1h": 1, "4h": 4}``.
    """

    horizons: Mapping[str, int] = field(default_factory=lambda: dict(DEFAULT_TRANSFORMER_HORIZONS))
    sequence_length: int = 256
    neutral_band: float = 0.002
    regime_volatility_threshold: float = 0.02
    bar_hours: float = 1.0 / 12.0

    def __post_init__(self) -> None:
        horizons = {str(name).strip(): max(1, int(value)) for name, value in dict(self.horizons).items() if str(name).strip()}
        if not horizons:
            raise ValueError("horizons must contain at least one horizon")
        if any(value <= 0 for value in horizons.values()):
            raise ValueError("horizon bars must be positive")
        object.__setattr__(self, "horizons", horizons)
        object.__setattr__(self, "sequence_length", max(8, int(self.sequence_length)))
        object.__setattr__(self, "neutral_band", max(0.0, float(self.neutral_band)))
        object.__setattr__(self, "regime_volatility_threshold", max(0.0, float(self.regime_volatility_threshold)))
        object.__setattr__(self, "bar_hours", max(1.0 / 3600.0, float(self.bar_hours)))


def _normalize_bars(bars: pd.DataFrame) -> pd.DataFrame:
    if bars is None or bars.empty:
        return pd.DataFrame()
    frame = bars.copy()
    timestamp_column = "date" if "date" in frame.columns else "timestamp" if "timestamp" in frame.columns else None
    if timestamp_column is None:
        raise ValueError("bars must include a date or timestamp column")
    frame["date"] = pd.to_datetime(frame[timestamp_column], utc=True, errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        if column not in frame.columns:
            raise ValueError(f"bars missing required column: {column}")
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["date", "open", "high", "low", "close"])
    frame = frame.sort_values("date").drop_duplicates("date", keep="last")
    frame = frame[(frame["open"] > 0) & (frame["close"] > 0) & (frame["high"] > 0) & (frame["low"] > 0)]
    return frame.reset_index(drop=True)


def _closed_bars(frame: pd.DataFrame, *, fetched_at: Any, bar_hours: float) -> pd.DataFrame:
    if frame.empty or fetched_at is None:
        return frame
    snapshot = pd.to_datetime(fetched_at, utc=True, errors="coerce")
    if pd.isna(snapshot):
        return frame
    duration = pd.Timedelta(hours=max(bar_hours, 1.0 / 3600.0))
    return frame.loc[frame["date"] + duration <= snapshot].copy()


def _rsi(close: pd.Series, window: int = 14) -> pd.Series:
    delta = close.diff()
    gains = delta.clip(lower=0).rolling(window, min_periods=window).mean()
    losses = (-delta.clip(upper=0)).rolling(window, min_periods=window).mean()
    rs = gains / losses.replace(0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


def _regime(return_value: Any, volatility: Any, config: TransformerFeatureConfig) -> Optional[str]:
    if pd.isna(return_value) or pd.isna(volatility):
        return None
    if float(volatility) >= config.regime_volatility_threshold:
        return "high_volatility"
    if float(return_value) > config.neutral_band:
        return "trend_up"
    if float(return_value) < -config.neutral_band:
        return "trend_down"
    return "sideways"


def build_transformer_feature_frame(
    bars: pd.DataFrame,
    *,
    config: Optional[TransformerFeatureConfig] = None,
    as_of: Any = None,
) -> pd.DataFrame:
    """Return causal features plus future multi-task labels.

    Feature columns are prefixed with ``feature_`` and labels with
    ``target_``.  A fetched timestamp is honored so a currently forming bar
    cannot enter either rolling indicators or a sequence window.
    """

    cfg = config or TransformerFeatureConfig()
    frame = _normalize_bars(bars)
    if frame.empty:
        return pd.DataFrame()
    cutoff = as_of if as_of is not None else getattr(bars, "attrs", {}).get("fetched_at")
    frame = _closed_bars(frame, fetched_at=cutoff, bar_hours=cfg.bar_hours)
    if frame.empty:
        return pd.DataFrame()

    close = frame["close"].astype(float)
    open_price = frame["open"].astype(float)
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    volume = pd.to_numeric(frame["volume"], errors="coerce").fillna(0.0).astype(float)
    log_return = np.log(close).diff()
    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()], axis=1
    ).max(axis=1)
    result = pd.DataFrame({"date": frame["date"], "reference_close": close})

    # Stationary price/volume geometry is more useful to a small model than
    # raw prices, and every operation is backward-looking.
    result["feature_log_return_1"] = log_return
    result["feature_open_close_return"] = close / open_price - 1.0
    result["feature_high_low_range"] = (high - low) / close
    result["feature_upper_wick"] = (high - pd.concat([open_price, close], axis=1).max(axis=1)) / close
    result["feature_lower_wick"] = (pd.concat([open_price, close], axis=1).min(axis=1) - low) / close
    result["feature_volume_log1p"] = np.log1p(volume.clip(lower=0))
    result["feature_volume_change"] = volume.pct_change().replace([np.inf, -np.inf], np.nan)
    result["feature_rsi_14"] = _rsi(close)
    result["feature_atr_pct_14"] = true_range.rolling(14, min_periods=14).mean() / close
    result["feature_macd"] = close.ewm(span=12, adjust=False).mean() / close.ewm(span=26, adjust=False).mean() - 1.0
    result["feature_macd_signal"] = result["feature_macd"].ewm(span=9, adjust=False).mean()
    result["feature_ema_slope_12"] = close.ewm(span=12, adjust=False).mean().pct_change(3)

    windows = (3, 6, 12, 24, 48, 96, 192)
    for window in windows:
        result[f"feature_return_mean_{window}"] = log_return.rolling(window, min_periods=window).mean()
        result[f"feature_return_std_{window}"] = log_return.rolling(window, min_periods=window).std(ddof=0)
        result[f"feature_momentum_{window}"] = close / close.shift(window) - 1.0
        volume_mean = volume.rolling(window, min_periods=window).mean()
        volume_std = volume.rolling(window, min_periods=window).std(ddof=0).replace(0, np.nan)
        result[f"feature_volume_zscore_{window}"] = (volume - volume_mean) / volume_std
        rolling_high = high.rolling(window, min_periods=window).max()
        rolling_low = low.rolling(window, min_periods=window).min()
        result[f"feature_range_position_{window}"] = (close - rolling_low) / (rolling_high - rolling_low).replace(0, np.nan)

    typical_price = (high + low + close) / 3.0
    vwap_window = 24
    volume_sum = volume.rolling(vwap_window, min_periods=vwap_window).sum().replace(0, np.nan)
    result["feature_vwap_deviation_24"] = close / ((typical_price * volume).rolling(vwap_window, min_periods=vwap_window).sum() / volume_sum) - 1.0
    result["feature_bollinger_position_24"] = (
        (close - close.rolling(24, min_periods=24).mean())
        / (2.0 * close.rolling(24, min_periods=24).std(ddof=0).replace(0, np.nan))
    )

    # Preserve aligned derivatives and cross-asset inputs when present.  No
    # synthetic neutral values are created for missing optional columns.
    external_columns = (
        "funding_rate", "open_interest", "basis", "liquidation_long", "liquidation_short",
        "long_short_ratio", "bid_depth", "ask_depth", "spread", "ofi", "eth_close", "sol_close",
        "dxy_close", "nasdaq_close", "vix_close",
    )
    for column in external_columns:
        if column not in frame.columns:
            continue
        series = pd.to_numeric(frame[column], errors="coerce")
        result[f"feature_{column}"] = series
        if column.endswith("_close") or column in {"open_interest", "basis", "bid_depth", "ask_depth"}:
            result[f"feature_{column}_change"] = series.pct_change().replace([np.inf, -np.inf], np.nan)

    for name, horizon in cfg.horizons.items():
        future_return = np.log(close.shift(-horizon) / close)
        next_returns = pd.concat([log_return.shift(-offset) for offset in range(1, horizon + 1)], axis=1)
        future_volatility = np.sqrt(next_returns.pow(2).mean(axis=1, skipna=False))
        result[f"target_return_{name}"] = future_return
        result[f"target_direction_{name}"] = np.select(
            [future_return > cfg.neutral_band, future_return < -cfg.neutral_band], [1, -1], default=0
        ).astype(float)
        result[f"target_volatility_{name}"] = future_volatility
        result[f"target_regime_{name}"] = [
            _regime(return_value, volatility, cfg)
            for return_value, volatility in zip(future_return, future_volatility)
        ]

    return result.replace([np.inf, -np.inf], np.nan)


__all__ = ["DEFAULT_TRANSFORMER_HORIZONS", "REGIME_LABELS", "TransformerFeatureConfig", "build_transformer_feature_frame"]
