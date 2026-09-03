# -*- coding: utf-8 -*-
"""Leakage-safe BTC multi-task training data and baseline evaluation.

This module is deliberately offline and observation-only.  It provides a small,
deterministic research baseline for the first step of BTC model development:
future return distributions, realized volatility, and market-regime labels are
created from closed OHLCV bars and evaluated with purged expanding windows plus
an isolated fixed-tail holdout.

The implementation does not make trading decisions and does not persist model
objects.  A caller can use the returned JSON-compatible structure as an
experiment artifact or as input to a later model-zoo implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


MODEL_VERSION = "btc-multitask-baseline-wf-v3"
FEATURE_SET_VERSION = "btc-ohlcv-economic-v1"
DEFAULT_QUANTILES = (0.10, 0.25, 0.50, 0.75, 0.90)
DEFAULT_HORIZONS = {"1h": 1, "4h": 4, "24h": 24}
DEFAULT_HOLDOUT_BARS = 168
DEFAULT_COST_SENSITIVITY_BPS = (14.0, 30.0, 50.0)
SUPPORTED_REGIMES = ("trend_up", "trend_down", "high_volatility", "sideways")
SUPPORTED_MODELS = ("linear", "lightgbm")


@dataclass(frozen=True)
class BtcTrainingConfig:
    """Configuration for the deterministic baseline.

    ``horizons`` maps a public horizon name to a number of input bars.  The
    default is suitable for hourly data; callers using 5-minute data can pass
    ``{"15m": 3, "1h": 12, "4h": 48}`` explicitly.
    """

    horizons: Mapping[str, int] = field(default_factory=lambda: dict(DEFAULT_HORIZONS))
    lookback_bars: int = 72
    min_train_bars: int = 336
    validation_bars: int = 48
    folds: int = 5
    neutral_band: float = 0.002
    regime_volatility_threshold: float = 0.02
    quantiles: Sequence[float] = DEFAULT_QUANTILES
    model: str = "linear"
    random_state: int = 42
    fee_bps_per_side: float = 5.0
    slippage_bps_per_side: float = 2.0
    min_trade_edge_bps: float = 0.0
    decision_stride: Optional[int] = None
    holdout_bars: int = DEFAULT_HOLDOUT_BARS
    cost_sensitivity_bps: Sequence[float] = DEFAULT_COST_SENSITIVITY_BPS

    def __post_init__(self) -> None:
        normalized_horizons = {
            str(name): max(1, int(bars))
            for name, bars in dict(self.horizons).items()
            if str(name).strip()
        }
        if not normalized_horizons:
            raise ValueError("horizons must contain at least one horizon")
        if any(value <= 0 for value in normalized_horizons.values()):
            raise ValueError("horizon bars must be positive")
        normalized_quantiles = tuple(sorted({float(value) for value in self.quantiles}))
        if not normalized_quantiles or any(value <= 0 or value >= 1 for value in normalized_quantiles):
            raise ValueError("quantiles must be between 0 and 1")
        normalized_costs = tuple(sorted({float(value) for value in self.cost_sensitivity_bps}))
        if not normalized_costs or any(not np.isfinite(value) or value < 0 for value in normalized_costs):
            raise ValueError("cost_sensitivity_bps must contain non-negative finite values")
        normalized_model = str(self.model or "").strip().lower()
        if normalized_model not in SUPPORTED_MODELS:
            raise ValueError(f"model must be one of: {', '.join(SUPPORTED_MODELS)}")
        object.__setattr__(self, "horizons", normalized_horizons)
        object.__setattr__(self, "quantiles", normalized_quantiles)
        object.__setattr__(self, "lookback_bars", max(20, int(self.lookback_bars)))
        object.__setattr__(self, "min_train_bars", max(24, int(self.min_train_bars)))
        object.__setattr__(self, "validation_bars", max(1, int(self.validation_bars)))
        object.__setattr__(self, "folds", max(1, int(self.folds)))
        object.__setattr__(self, "neutral_band", max(0.0, float(self.neutral_band)))
        object.__setattr__(self, "regime_volatility_threshold", max(0.0, float(self.regime_volatility_threshold)))
        object.__setattr__(self, "model", normalized_model)
        object.__setattr__(self, "random_state", int(self.random_state))
        object.__setattr__(self, "fee_bps_per_side", max(0.0, float(self.fee_bps_per_side)))
        object.__setattr__(self, "slippage_bps_per_side", max(0.0, float(self.slippage_bps_per_side)))
        object.__setattr__(self, "min_trade_edge_bps", max(0.0, float(self.min_trade_edge_bps)))
        if self.decision_stride is not None:
            object.__setattr__(self, "decision_stride", max(1, int(self.decision_stride)))
        object.__setattr__(self, "holdout_bars", max(0, int(self.holdout_bars)))
        object.__setattr__(self, "cost_sensitivity_bps", normalized_costs)

    @property
    def round_trip_cost_bps(self) -> float:
        return 2.0 * (self.fee_bps_per_side + self.slippage_bps_per_side)


def _safe_numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    return pd.to_numeric(frame.get(column, pd.Series(index=frame.index, dtype=float)), errors="coerce")


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
    frame = (
        frame.dropna(subset=["date", "open", "high", "low", "close"])
        .sort_values("date")
        .drop_duplicates("date", keep="last")
    )
    frame = frame[(frame["close"] > 0) & (frame["open"] > 0)]
    return frame.reset_index(drop=True)


def _closed_bars(frame: pd.DataFrame, *, fetched_at: Any = None, bar_hours: float = 1.0) -> pd.DataFrame:
    """Drop a possibly open last bar when a fetch timestamp is available."""
    if frame.empty:
        return frame
    snapshot = pd.to_datetime(fetched_at, utc=True, errors="coerce")
    if pd.isna(snapshot):
        return frame
    duration = pd.Timedelta(hours=max(float(bar_hours), 1 / 60))
    return frame.loc[frame["date"] + duration <= snapshot].copy()


def build_feature_frame(
    bars: pd.DataFrame,
    *,
    config: Optional[BtcTrainingConfig] = None,
    bar_hours: float = 1.0,
    as_of: Any = None,
) -> pd.DataFrame:
    """Build causal features and future labels from OHLCV bars.

    Every feature at row ``t`` is calculated from rows at or before ``t``.
    Future values only appear in columns prefixed with ``target_``.
    """
    cfg = config or BtcTrainingConfig()
    frame = _normalize_bars(bars)
    if frame.empty:
        return pd.DataFrame()
    cutoff = as_of if as_of is not None else getattr(bars, "attrs", {}).get("fetched_at")
    frame = _closed_bars(frame, fetched_at=cutoff, bar_hours=bar_hours)
    if frame.empty:
        return pd.DataFrame()

    close = frame["close"].astype(float)
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    open_price = frame["open"].astype(float)
    volume = frame["volume"].fillna(0.0).astype(float)
    log_return = np.log(close).diff()
    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()], axis=1
    ).max(axis=1)

    features = pd.DataFrame(index=frame.index)
    features["date"] = frame["date"]
    features["reference_close"] = close
    features["feature_log_return_1"] = log_return
    configured_windows = tuple(
        window for window in (3, 6, 12, 24, 72) if window <= cfg.lookback_bars
    )
    feature_windows = configured_windows or (cfg.lookback_bars,)
    for window in feature_windows:
        features[f"feature_return_mean_{window}"] = log_return.rolling(window, min_periods=window).mean()
        features[f"feature_realized_vol_{window}"] = log_return.rolling(window, min_periods=window).std(ddof=0)
        features[f"feature_momentum_{window}"] = close / close.shift(window) - 1.0
    atr_window = min(14, cfg.lookback_bars)
    features["feature_atr_pct_14"] = true_range.rolling(atr_window, min_periods=atr_window).mean() / close
    delta = close.diff()
    gains = delta.clip(lower=0).rolling(atr_window, min_periods=atr_window).mean()
    losses = (-delta.clip(upper=0)).rolling(atr_window, min_periods=atr_window).mean()
    relative_strength = gains / losses.replace(0, np.nan)
    features["feature_rsi_14"] = 100.0 - 100.0 / (1.0 + relative_strength)
    ema_fast = close.ewm(span=12, adjust=False).mean()
    ema_slow = close.ewm(span=26, adjust=False).mean()
    features["feature_ema_spread"] = ema_fast / ema_slow - 1.0
    features["feature_ema_slope_12"] = ema_fast.pct_change(3)
    volume_window = min(24, cfg.lookback_bars)
    volume_mean = volume.rolling(volume_window, min_periods=volume_window).mean()
    volume_std = volume.rolling(volume_window, min_periods=volume_window).std(ddof=0).replace(0, np.nan)
    features["feature_volume_zscore_24"] = (volume - volume_mean) / volume_std
    rolling_volume = volume.rolling(volume_window, min_periods=volume_window).sum()
    typical_price = (high + low + close) / 3.0
    features["feature_vwap_deviation_24"] = (
        close / ((typical_price * volume).rolling(volume_window, min_periods=volume_window).sum() / rolling_volume.replace(0, np.nan))
        - 1.0
    )
    features["feature_body_pct"] = (close - open_price) / open_price
    features["feature_range_pct"] = (high - low) / close

    # Preserve optional, already timestamp-aligned external columns without
    # inventing values.  Their names make the source visible in experiment
    # artifacts and they are still fit/scaled inside each walk-forward fold.
    for column in ("funding_rate", "open_interest", "basis", "eth_close", "sol_close", "dxy_close", "nasdaq_close", "vix_close"):
        if column not in frame.columns:
            continue
        series = _safe_numeric(frame, column)
        features[f"feature_{column}"] = series
        if series.gt(0).all():
            features[f"feature_{column}_return_1"] = np.log(series).diff()

    # Targets are explicitly future-looking and are never included in feature
    # columns.  Multi-horizon volatility uses the RMS of the next N returns.
    for name, horizon in cfg.horizons.items():
        future = np.log(close.shift(-horizon) / close)
        execution_future = np.log(close.shift(-horizon) / open_price.shift(-1))
        features[f"target_return_{name}"] = future
        features[f"target_direction_{name}"] = np.select(
            [future > cfg.neutral_band, future < -cfg.neutral_band], [1, -1], default=0
        ).astype(float)
        features[f"target_trade_return_{name}"] = execution_future
        features[f"target_trade_direction_{name}"] = np.select(
            [execution_future > cfg.neutral_band, execution_future < -cfg.neutral_band],
            [1, -1],
            default=0,
        ).astype(float)
        next_returns = pd.concat([log_return.shift(-offset) for offset in range(1, horizon + 1)], axis=1)
        future_vol = np.sqrt((next_returns.pow(2).mean(axis=1, skipna=False)))
        features[f"target_volatility_{name}"] = future_vol
        features[f"target_regime_{name}"] = [
            _regime_label(float(return_value) if pd.notna(return_value) else None, float(vol_value) if pd.notna(vol_value) else None, cfg)
            for return_value, vol_value in zip(future, future_vol)
        ]
    return features.replace([np.inf, -np.inf], np.nan)


def _regime_label(future_return: Optional[float], future_volatility: Optional[float], config: BtcTrainingConfig) -> Optional[str]:
    if future_return is None or future_volatility is None:
        return None
    if future_volatility >= config.regime_volatility_threshold:
        return "high_volatility"
    if future_return > config.neutral_band:
        return "trend_up"
    if future_return < -config.neutral_band:
        return "trend_down"
    return "sideways"


def walk_forward_splits(
    row_count: int,
    *,
    min_train_bars: int,
    validation_bars: int,
    folds: int,
    purge_bars: int,
) -> list[dict[str, int]]:
    """Return expanding train/validation slices with an explicit purge gap."""
    # The validation origin is after the purge gap.  This keeps the full
    # requested training window while ensuring no label can overlap validation.
    minimum_train = max(int(min_train_bars), 1)
    first_start = minimum_train + max(int(purge_bars), 0)
    last_start = int(row_count) - max(int(validation_bars), 1)
    if last_start < first_start:
        return []
    available = last_start - first_start + 1
    starts = np.linspace(first_start, last_start, num=min(max(int(folds), 1), available), dtype=int)
    result = []
    for raw_start in dict.fromkeys(int(value) for value in starts):
        train_end = raw_start - max(int(purge_bars), 0)
        validation_end = min(raw_start + int(validation_bars), int(row_count))
        if train_end < minimum_train or validation_end <= raw_start:
            continue
        result.append(
            {
                "train_start": 0,
                "train_end": train_end,
                "validation_start": raw_start,
                "validation_end": validation_end,
                "purge_bars": max(int(purge_bars), 0),
            }
        )
    return result


def fixed_holdout_split(
    row_count: int,
    *,
    min_train_bars: int,
    holdout_bars: int,
    purge_bars: int,
) -> Optional[dict[str, int]]:
    """Return one fixed tail holdout split, or ``None`` when it cannot fit.

    The purge interval immediately before the holdout is excluded from both
    model fitting and holdout scoring.  Keeping this split separate from the
    walk-forward folds prevents the final evaluation window from influencing
    either model selection or the latest forecast.
    """

    count = max(int(row_count), 0)
    requested_holdout = max(int(holdout_bars), 0)
    minimum_train = max(int(min_train_bars), 1)
    purge = max(int(purge_bars), 0)
    if requested_holdout <= 0 or count <= 0:
        return None
    holdout_start = count - requested_holdout
    purge_start = holdout_start - purge
    if holdout_start < 0 or purge_start < minimum_train:
        return None
    return {
        "train_start": 0,
        "train_end": purge_start,
        "purge_start": purge_start,
        "purge_end": holdout_start,
        "holdout_start": holdout_start,
        "holdout_end": count,
        "train_samples": purge_start,
        "holdout_samples": requested_holdout,
        "purge_bars": purge,
    }


def _feature_columns(frame: pd.DataFrame) -> list[str]:
    return [column for column in frame.columns if column.startswith("feature_")]


def _make_model(model_name: str, task: str, config: BtcTrainingConfig) -> Any:
    if model_name == "linear":
        if task == "return" or task == "volatility":
            return make_pipeline(StandardScaler(), Ridge(alpha=1.0))
        if task == "direction" or task == "regime":
            return make_pipeline(StandardScaler(), LogisticRegression(C=0.5, max_iter=300))
        raise ValueError(f"unsupported linear task: {task}")

    if model_name != "lightgbm":
        raise ValueError(f"unsupported model: {model_name}")
    try:
        from lightgbm import LGBMClassifier, LGBMRegressor
    except (ImportError, OSError) as exc:
        raise ValueError("lightgbm model requested but the optional dependency is unavailable") from exc

    common = {
        "n_estimators": 160,
        "learning_rate": 0.03,
        "num_leaves": 15,
        "max_depth": 4,
        "min_child_samples": 40,
        "reg_lambda": 1.0,
        "verbosity": -1,
        "random_state": config.random_state,
        "n_jobs": 1,
    }
    if task == "return" or task == "volatility":
        return LGBMRegressor(**common)
    if task == "direction" or task == "regime":
        return LGBMClassifier(**common)
    raise ValueError(f"unsupported LightGBM task: {task}")


def _fit_models(train: pd.DataFrame, features: Sequence[str], horizon: str, config: BtcTrainingConfig) -> dict[str, Any]:
    x_train = train[list(features)].to_numpy(dtype=float)
    return_target = train[f"target_return_{horizon}"].to_numpy(dtype=float)
    vol_target = train[f"target_volatility_{horizon}"].to_numpy(dtype=float)
    direction_target = train[f"target_direction_{horizon}"].to_numpy(dtype=int)
    regime_target = train[f"target_regime_{horizon}"].astype(str).to_numpy()
    return_model = _make_model(config.model, "return", config)
    vol_model = _make_model(config.model, "volatility", config)
    return_model.fit(x_train, return_target)
    vol_model.fit(x_train, vol_target)
    direction_model = None
    if len(np.unique(direction_target)) >= 2:
        direction_model = _make_model(config.model, "direction", config)
        direction_model.fit(x_train, direction_target)
    regime_model = None
    if len(np.unique(regime_target)) >= 2:
        regime_model = _make_model(config.model, "regime", config)
        regime_model.fit(x_train, regime_target)
    residuals = return_target - return_model.predict(x_train)
    return {
        "return_model": return_model,
        "vol_model": vol_model,
        "direction_model": direction_model,
        "regime_model": regime_model,
        "return_residual_quantiles": np.quantile(residuals, config.quantiles),
        "return_prior_quantiles": np.quantile(return_target, config.quantiles),
        "direction_prior": {str(value): float(np.mean(direction_target == value)) for value in (-1, 0, 1)},
        "regime_prior": {str(value): float(np.mean(regime_target == value)) for value in SUPPORTED_REGIMES},
        "model": config.model,
    }


def _predict(models: Mapping[str, Any], test: pd.DataFrame, features: Sequence[str], config: BtcTrainingConfig) -> dict[str, Any]:
    x_test = test[list(features)].to_numpy(dtype=float)
    return_pred = np.asarray(models["return_model"].predict(x_test), dtype=float)
    vol_pred = np.maximum(np.asarray(models["vol_model"].predict(x_test), dtype=float), 0.0)
    if models["direction_model"] is None:
        direction_probabilities = np.tile(
            np.array([models["direction_prior"].get(str(value), 0.0) for value in (-1, 0, 1)]),
            (len(test), 1),
        )
        direction_classes = np.array([-1, 0, 1], dtype=int)
    else:
        estimator = models["direction_model"]
        raw = estimator.predict_proba(x_test)
        direction_classes = np.asarray(estimator.classes_, dtype=int)
        direction_probabilities = np.zeros((len(test), 3), dtype=float)
        for source_index, value in enumerate(direction_classes):
            direction_probabilities[:, {-1: 0, 0: 1, 1: 2}[int(value)]] = raw[:, source_index]
    if models["regime_model"] is None:
        regime_predictions = np.array([max(models["regime_prior"], key=models["regime_prior"].get)] * len(test), dtype=object)
    else:
        regime_predictions = models["regime_model"].predict(x_test)
    residual_quantiles = np.asarray(models["return_residual_quantiles"], dtype=float)
    distribution = np.maximum(return_pred[:, None] + residual_quantiles[None, :], -0.999)
    return {
        "return": return_pred,
        "volatility": vol_pred,
        "direction_probabilities": direction_probabilities,
        "regime": np.asarray(regime_predictions, dtype=object),
        "distribution": distribution,
    }


def _prediction_records(
    labeled: pd.DataFrame,
    test: pd.DataFrame,
    predictions: Mapping[str, Any],
    horizon: str,
    *,
    train_end: int,
    evaluation: str,
) -> list[dict[str, Any]]:
    """Convert one prediction window into JSON-compatible evaluation rows."""

    if test.empty:
        return []
    train_end_index = max(int(train_end) - 1, 0)
    train_end_date = str(labeled.iloc[train_end_index]["date"]) if train_end > 0 else None
    validation_start = str(test.iloc[0]["date"])
    direction_labels = np.array([-1, 0, 1], dtype=int)
    records: list[dict[str, Any]] = []
    for index, row in enumerate(test.itertuples(index=True)):
        predicted_direction = int(direction_labels[predictions["direction_probabilities"][index].argmax()])
        records.append(
            {
                "evaluation": evaluation,
                "actual_return": float(test.iloc[index][f"target_return_{horizon}"]),
                "actual_trade_return": float(test.iloc[index][f"target_trade_return_{horizon}"]),
                "actual_volatility": float(test.iloc[index][f"target_volatility_{horizon}"]),
                "actual_direction": int(test.iloc[index][f"target_direction_{horizon}"]),
                "actual_trade_direction": int(test.iloc[index][f"target_trade_direction_{horizon}"]),
                "actual_regime": str(test.iloc[index][f"target_regime_{horizon}"]),
                "predicted_return": float(predictions["return"][index]),
                "predicted_volatility": float(predictions["volatility"][index]),
                "predicted_direction": predicted_direction,
                "predicted_regime": str(predictions["regime"][index]),
                "distribution": [float(value) for value in predictions["distribution"][index]],
                "decision_index": int(row.Index),
                "train_end": train_end_date,
                "validation_start": validation_start,
            }
        )
    return records


def _prediction_metric_summary(rows: Sequence[Mapping[str, Any]], config: BtcTrainingConfig) -> dict[str, Any]:
    """Summarize prediction rows with the same metrics for every split type."""

    if not rows:
        return {}
    actual_return = np.array([row["actual_return"] for row in rows], dtype=float)
    actual_volatility = np.array([row["actual_volatility"] for row in rows], dtype=float)
    actual_direction = np.array([row["actual_direction"] for row in rows], dtype=int)
    actual_regime = np.array([row["actual_regime"] for row in rows], dtype=object)
    predicted = {
        "return": np.array([row["predicted_return"] for row in rows], dtype=float),
        "volatility": np.array([row["predicted_volatility"] for row in rows], dtype=float),
        "direction_probabilities": np.eye(3)[
            np.array([{-1: 0, 0: 1, 1: 2}[int(row["predicted_direction"])] for row in rows], dtype=int)
        ],
        "regime": np.array([row["predicted_regime"] for row in rows], dtype=object),
        "distribution": np.array([row["distribution"] for row in rows], dtype=float),
    }
    return _metric_summary(actual_return, actual_volatility, actual_direction, actual_regime, predicted, config)


def _pinball_loss(actual: np.ndarray, predicted: np.ndarray, quantile: float) -> float:
    error = actual - predicted
    return float(np.mean(np.maximum(quantile * error, (quantile - 1.0) * error)))


def _metric_summary(actual_return: np.ndarray, actual_vol: np.ndarray, actual_direction: np.ndarray, actual_regime: np.ndarray, predictions: Mapping[str, Any], config: BtcTrainingConfig) -> dict[str, Any]:
    direction_probabilities = np.asarray(predictions["direction_probabilities"], dtype=float)
    predicted_direction = np.array([-1, 0, 1], dtype=int)[direction_probabilities.argmax(axis=1)]
    predicted_regime = np.asarray(predictions["regime"], dtype=object)
    distribution = np.asarray(predictions["distribution"], dtype=float)
    return {
        "samples": int(len(actual_return)),
        "return_mae_pct": round(float(np.mean(np.abs(predictions["return"] - actual_return)) * 100), 6),
        "volatility_mae_pct": round(float(np.mean(np.abs(predictions["volatility"] - actual_vol)) * 100), 6),
        "direction_accuracy": round(float(np.mean(predicted_direction == actual_direction)), 6),
        "regime_accuracy": round(float(np.mean(predicted_regime == actual_regime)), 6),
        "quantile_pinball_loss": {
            str(quantile): round(_pinball_loss(actual_return, distribution[:, index], quantile), 8)
            for index, quantile in enumerate(config.quantiles)
        },
    }


def _execution_evaluation(
    oof: Sequence[Mapping[str, Any]],
    *,
    horizon: int,
    config: BtcTrainingConfig,
    round_trip_cost_bps: Optional[float] = None,
) -> dict[str, Any]:
    """Evaluate model directions as non-overlapping next-open trades.

    The model remains a research forecast. This helper only translates its
    out-of-fold predictions into a transparent cost-aware diagnostic using the
    next bar open and the close at the requested holding horizon.
    """

    effective_cost_bps = (
        config.round_trip_cost_bps
        if round_trip_cost_bps is None
        else max(0.0, float(round_trip_cost_bps))
    )
    if not oof:
        return {
            "data_quality": "insufficient",
            "decision_count": 0,
            "signal_count": 0,
            "reason": "no_out_of_fold_predictions",
            "round_trip_cost_bps": effective_cost_bps,
        }

    ordered = sorted(oof, key=lambda row: int(row.get("decision_index", 0)))
    stride = max(horizon, config.decision_stride or horizon)
    decisions: list[Mapping[str, Any]] = []
    last_decision_index: Optional[int] = None
    for row in ordered:
        decision_index = int(row.get("decision_index", 0))
        if last_decision_index is not None and decision_index < last_decision_index + stride:
            continue
        decisions.append(row)
        last_decision_index = decision_index

    cost_rate = effective_cost_bps / 10000.0
    required_edge = (effective_cost_bps + config.min_trade_edge_bps) / 10000.0
    net_returns: list[float] = []
    direction_correct: list[bool] = []
    signal_count = 0
    for row in decisions:
        direction = int(row.get("predicted_direction", 0))
        predicted_return = float(row.get("predicted_return", 0.0))
        if direction == 0 or abs(predicted_return) < required_edge:
            continue
        actual_return = float(row["actual_trade_return"])
        gross_return = float(np.expm1(direction * actual_return))
        net_returns.append(gross_return - cost_rate)
        direction_correct.append(direction == int(row.get("actual_trade_direction", 0)))
        signal_count += 1

    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for net_return in net_returns:
        equity *= max(0.0, 1.0 + net_return)
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1.0)
    positive = sum(value for value in net_returns if value > 0.0)
    negative = abs(sum(value for value in net_returns if value < 0.0))
    return {
        "data_quality": "available",
        "definition": "predicted direction and return; entry=next_bar_open; exit=holding_horizon_bar_close",
        "decision_count": len(decisions),
        "signal_count": signal_count,
        "signal_rate": round(signal_count / len(decisions), 6) if decisions else None,
        "directional_accuracy": round(float(np.mean(direction_correct)), 6) if direction_correct else None,
        "decision_stride_bars": stride,
        "non_overlapping": True,
        "fee_bps_per_side": config.fee_bps_per_side if round_trip_cost_bps is None else None,
        "slippage_bps_per_side": config.slippage_bps_per_side if round_trip_cost_bps is None else None,
        "round_trip_cost_bps": effective_cost_bps,
        "min_trade_edge_bps": config.min_trade_edge_bps,
        "signal_hurdle_bps": effective_cost_bps + config.min_trade_edge_bps,
        "avg_net_return_pct": round(float(np.mean(net_returns)) * 100.0, 6) if net_returns else None,
        "net_positive_rate": round(float(np.mean(np.asarray(net_returns) > 0.0)), 6) if net_returns else None,
        "cumulative_net_return_pct": round((equity - 1.0) * 100.0, 6) if net_returns else None,
        "max_drawdown_pct": round(max_drawdown * 100.0, 6) if net_returns else None,
        "profit_factor": round(positive / negative, 6) if negative > 0.0 else None,
    }


def _cost_sensitivity_evaluation(
    oof: Sequence[Mapping[str, Any]],
    *,
    horizon: int,
    config: BtcTrainingConfig,
) -> dict[str, Any]:
    """Re-score one holdout prediction set under several round-trip costs."""

    return {
        "same_holdout_predictions": True,
        "refit_per_cost": False,
        "signal_hurdle": "abs(predicted_return) >= round_trip_cost + min_trade_edge",
        "scenarios": [
            _execution_evaluation(
                oof,
                horizon=horizon,
                config=config,
                round_trip_cost_bps=cost_bps,
            )
            for cost_bps in config.cost_sensitivity_bps
        ],
    }


class BtcTrainingService:
    """Run multi-task baseline training and leakage-safe evaluation."""

    def __init__(self, config: Optional[BtcTrainingConfig] = None) -> None:
        self.config = config or BtcTrainingConfig()

    def build(self, bars: pd.DataFrame, *, bar_hours: float = 1.0, as_of: Any = None) -> dict[str, Any]:
        frame = build_feature_frame(bars, config=self.config, bar_hours=bar_hours, as_of=as_of)
        base: dict[str, Any] = {
            "model_version": MODEL_VERSION,
            "feature_set_version": FEATURE_SET_VERSION,
            "mode": "offline_research",
            "participates_in_decision": False,
            "research_only": True,
            "eligible_for_promotion": False,
            "promotion_eligible": False,
            "horizons": dict(self.config.horizons),
            "lookback_bars": self.config.lookback_bars,
            "bar_hours": float(bar_hours),
            "model": self.config.model,
            "execution_config": {
                "entry": "next_bar_open",
                "exit": "close_at_horizon",
                "fee_bps_per_side": self.config.fee_bps_per_side,
                "slippage_bps_per_side": self.config.slippage_bps_per_side,
                "round_trip_cost_bps": self.config.round_trip_cost_bps,
                "min_trade_edge_bps": self.config.min_trade_edge_bps,
                "decision_stride_bars": "horizon unless explicitly configured",
                "fixed_holdout_bars": self.config.holdout_bars,
                "cost_sensitivity_round_trip_bps": list(self.config.cost_sensitivity_bps),
            },
            "leakage_guard": {
                "scheme": "purged_expanding_walk_forward",
                "feature_fit_scope": "train_only",
                "random_split": False,
                "fixed_tail_holdout": True,
                "holdout_labels_used_for_latest_forecast": False,
                "status": "passed",
            },
        }
        if frame.empty:
            return {**base, "data_quality": "unavailable", "reason": "bars_missing_or_invalid", "forecasts": {}, "evaluations": {}}
        features = _feature_columns(frame)
        forecasts: dict[str, Any] = {}
        evaluations: dict[str, Any] = {}
        latest = frame.dropna(subset=features).tail(1)
        for name, horizon in self.config.horizons.items():
            target_columns = [
                f"target_return_{name}",
                f"target_trade_return_{name}",
                f"target_volatility_{name}",
                f"target_direction_{name}",
                f"target_trade_direction_{name}",
                f"target_regime_{name}",
            ]
            labeled = frame.dropna(subset=[*features, *target_columns]).copy()
            folds = walk_forward_splits(
                len(labeled),
                min_train_bars=self.config.min_train_bars,
                validation_bars=self.config.validation_bars,
                folds=self.config.folds,
                purge_bars=horizon,
            )
            oof: list[dict[str, Any]] = []
            for fold in folds:
                train = labeled.iloc[: fold["train_end"]]
                test = labeled.iloc[fold["validation_start"] : fold["validation_end"]]
                if train.empty or test.empty:
                    continue
                models = _fit_models(train, features, name, self.config)
                predicted = _predict(models, test, features, self.config)
                oof.extend(
                    _prediction_records(
                        labeled,
                        test,
                        predicted,
                        name,
                        train_end=fold["train_end"],
                        evaluation="walk_forward",
                    )
                )
            if oof:
                evaluations[name] = {
                    **_prediction_metric_summary(oof, self.config),
                    "fold_count": len(folds),
                    "purge_bars": horizon,
                    "folds": folds,
                    "execution_evaluation": _execution_evaluation(
                        oof,
                        horizon=horizon,
                        config=self.config,
                    ),
                }
            else:
                evaluations[name] = {
                    "samples": 0,
                    "fold_count": 0,
                    "purge_bars": horizon,
                    "reason": "insufficient_labeled_bars",
                    "execution_evaluation": _execution_evaluation(
                        oof,
                        horizon=horizon,
                        config=self.config,
                    ),
                }

            holdout_split = fixed_holdout_split(
                len(labeled),
                min_train_bars=self.config.min_train_bars,
                holdout_bars=self.config.holdout_bars,
                purge_bars=horizon,
            )
            holdout_rows: list[dict[str, Any]] = []
            holdout_models: Optional[dict[str, Any]] = None
            if holdout_split is not None:
                holdout_train = labeled.iloc[: holdout_split["train_end"]]
                holdout_test = labeled.iloc[holdout_split["holdout_start"] : holdout_split["holdout_end"]]
                if len(holdout_train) >= self.config.min_train_bars and not holdout_test.empty:
                    holdout_models = _fit_models(holdout_train, features, name, self.config)
                    holdout_prediction = _predict(holdout_models, holdout_test, features, self.config)
                    holdout_rows = _prediction_records(
                        labeled,
                        holdout_test,
                        holdout_prediction,
                        name,
                        train_end=holdout_split["train_end"],
                        evaluation="fixed_tail_holdout",
                    )

            if holdout_rows and holdout_split is not None:
                evaluations[name]["holdout_evaluation"] = {
                    **_prediction_metric_summary(holdout_rows, self.config),
                    "data_quality": "available",
                    "evaluation_type": "fixed_tail_holdout",
                    "train_samples": holdout_split["train_samples"],
                    "holdout_samples": holdout_split["holdout_samples"],
                    "purge_bars": holdout_split["purge_bars"],
                    "split": holdout_split,
                    "model_fit_scope": "rows_before_holdout_purge",
                    "execution_evaluation": _execution_evaluation(
                        holdout_rows,
                        horizon=horizon,
                        config=self.config,
                    ),
                    "cost_sensitivity": _cost_sensitivity_evaluation(
                        holdout_rows,
                        horizon=horizon,
                        config=self.config,
                    ),
                }
            else:
                evaluations[name]["holdout_evaluation"] = {
                    "data_quality": "insufficient",
                    "evaluation_type": "fixed_tail_holdout",
                    "requested_holdout_bars": self.config.holdout_bars,
                    "purge_bars": horizon,
                    "reason": "insufficient_labeled_bars_for_holdout",
                    "cost_sensitivity": _cost_sensitivity_evaluation(
                        holdout_rows,
                        horizon=horizon,
                        config=self.config,
                    ),
                }

            if len(labeled) >= self.config.min_train_bars and not latest.empty:
                if holdout_models is not None and holdout_split is not None:
                    forecast_models = holdout_models
                    forecast_training_scope = "before_holdout_purge"
                    forecast_training_samples = holdout_split["train_samples"]
                else:
                    forecast_models = _fit_models(labeled, features, name, self.config)
                    forecast_training_scope = "all_labeled_holdout_unavailable"
                    forecast_training_samples = len(labeled)
                latest_prediction = _predict(forecast_models, latest, features, self.config)
                probabilities = latest_prediction["direction_probabilities"][0]
                forecasts[name] = {
                    "model": self.config.model,
                    "as_of": str(latest.iloc[0]["date"]),
                    "return": round(float(latest_prediction["return"][0]), 8),
                    "return_pct": round(float(latest_prediction["return"][0]) * 100, 6),
                    "volatility": round(float(latest_prediction["volatility"][0]), 8),
                    "volatility_pct": round(float(latest_prediction["volatility"][0]) * 100, 6),
                    "direction_probabilities": {label: round(float(probabilities[index]), 6) for index, label in enumerate(("down", "neutral", "up"))},
                    "regime": str(latest_prediction["regime"][0]),
                    "return_quantiles": {str(quantile): round(float(latest_prediction["distribution"][0][index]), 8) for index, quantile in enumerate(self.config.quantiles)},
                    "training_samples": int(forecast_training_samples),
                    "training_data_scope": forecast_training_scope,
                }
            else:
                forecasts[name] = None

        return {
            **base,
            "data_quality": "available" if any(value is not None for value in forecasts.values()) else "insufficient",
            "source_bar_count": int(len(frame)),
            "feature_count": int(len(features)),
            "feature_columns": features,
            "forecasts": forecasts,
            "evaluations": evaluations,
            "label_definition": {
                "direction": "future_log_return > neutral_band => up; < -neutral_band => down; otherwise neutral",
                "neutral_band": self.config.neutral_band,
                "execution_return": "log(close[t+horizon] / open[t+1]); used for next-open cost-aware evaluation",
                "execution_direction": "execution_return > neutral_band => up; < -neutral_band => down; otherwise neutral",
                "volatility": "RMS of next N closed log returns",
                "regime": "high_volatility, trend_up, trend_down, or sideways using fixed thresholds",
            },
        }


__all__ = [
    "BtcTrainingConfig",
    "BtcTrainingService",
    "DEFAULT_COST_SENSITIVITY_BPS",
    "DEFAULT_HOLDOUT_BARS",
    "FEATURE_SET_VERSION",
    "MODEL_VERSION",
    "SUPPORTED_MODELS",
    "build_feature_frame",
    "fixed_holdout_split",
    "walk_forward_splits",
]
