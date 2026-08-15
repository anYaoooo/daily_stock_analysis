# -*- coding: utf-8 -*-
"""Leakage-safe, observation-only BTC hourly return forecasts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

import numpy as np
import pandas as pd


MODEL_VERSION = "btc-hourly-shadow-wf-v1"
DEFAULT_LOOKBACK_DAYS = 60
DEFAULT_MIN_TRAIN_BARS = 336
DEFAULT_FOLDS = 5
DEFAULT_VALIDATION_BARS = 24


@dataclass(frozen=True)
class _FoldPrediction:
    actual_return: float
    predicted_return: float
    up_probability: float
    train_end_at: str
    validation_start_at: str


class BtcShadowForecastService:
    """Build one-step BTC forecasts without affecting trading decisions.

    The service intentionally uses small numpy models rather than notebook-only
    dependencies. Every validation fold fits its own scaler on prior data only.
    """

    def __init__(
        self,
        *,
        min_train_bars: int = DEFAULT_MIN_TRAIN_BARS,
        folds: int = DEFAULT_FOLDS,
        validation_bars: int = DEFAULT_VALIDATION_BARS,
    ) -> None:
        self.min_train_bars = max(24, int(min_train_bars))
        self.folds = max(1, int(folds))
        self.validation_bars = max(1, int(validation_bars))

    def build(self, hourly_bars: Optional[pd.DataFrame]) -> Dict[str, Any]:
        base = {
            "model_version": MODEL_VERSION,
            "mode": "shadow",
            "participates_in_decision": False,
            "target": "next_closed_1h_return",
            "bar_period": "hourly",
        }
        if hourly_bars is None or hourly_bars.empty:
            return self._unavailable(base, "hourly_bars_missing")

        features, closed_bar_count = self._feature_frame(hourly_bars)
        if features.empty:
            return self._unavailable(base, "insufficient_valid_hourly_bars", closed_bar_count)

        feature_columns = [column for column in features.columns if column.startswith("feature_")]
        labeled = features.dropna(subset=[*feature_columns, "target_return"]).copy()
        latest = features.dropna(subset=feature_columns).tail(1)
        required_labeled = self.min_train_bars + self.validation_bars
        if len(labeled) < required_labeled or latest.empty:
            return {
                **base,
                "data_quality": "insufficient",
                "reason": "insufficient_labeled_bars_for_walk_forward",
                "source_closed_bar_count": closed_bar_count,
                "usable_labeled_bar_count": int(len(labeled)),
                "minimum_required_labeled_bars": required_labeled,
                "forecast": None,
                "walk_forward": None,
            }

        fold_predictions = self._walk_forward(labeled, feature_columns)
        if not fold_predictions:
            return {
                **base,
                "data_quality": "insufficient",
                "reason": "no_valid_walk_forward_folds",
                "source_closed_bar_count": closed_bar_count,
                "usable_labeled_bar_count": int(len(labeled)),
                "forecast": None,
                "walk_forward": None,
            }

        x_train = labeled[feature_columns].to_numpy(dtype=float)
        y_return = labeled["target_return"].to_numpy(dtype=float)
        y_direction = (y_return > 0).astype(float)
        x_latest = latest[feature_columns].to_numpy(dtype=float)
        expected_returns, up_probabilities = self._fit_predict(
            x_train,
            y_return,
            y_direction,
            x_latest,
        )
        expected_return = float(expected_returns[0])
        up_probability = float(up_probabilities[0])
        direction = "up" if up_probability >= 0.5 else "down"

        return {
            **base,
            "data_quality": "available",
            "source_closed_bar_count": closed_bar_count,
            "usable_labeled_bar_count": int(len(labeled)),
            "feature_count": len(feature_columns),
            "forecast_as_of": self._timestamp(latest.index[-1]),
            "forecast": {
                "expected_return_pct": round(expected_return * 100, 4),
                "up_probability": round(up_probability, 4),
                "down_probability": round(1.0 - up_probability, 4),
                "predicted_direction": direction,
            },
            "walk_forward": self._walk_forward_summary(fold_predictions),
            "note": "仅用于影子观测与离线校准；不得作为交易方向、入场、仓位或执行触发依据。",
        }

    def _walk_forward(
        self,
        labeled: pd.DataFrame,
        feature_columns: Iterable[str],
    ) -> list[_FoldPrediction]:
        max_folds = (len(labeled) - self.min_train_bars) // self.validation_bars
        fold_count = min(self.folds, max_folds)
        if fold_count < 1:
            return []

        validation_start = len(labeled) - fold_count * self.validation_bars
        predictions: list[_FoldPrediction] = []
        columns = list(feature_columns)
        for fold_index in range(fold_count):
            start = validation_start + fold_index * self.validation_bars
            stop = start + self.validation_bars
            train = labeled.iloc[:start]
            validation = labeled.iloc[start:stop]
            if len(train) < self.min_train_bars or validation.empty:
                continue

            x_train = train[columns].to_numpy(dtype=float)
            y_return = train["target_return"].to_numpy(dtype=float)
            y_direction = (y_return > 0).astype(float)
            x_validation = validation[columns].to_numpy(dtype=float)
            predicted_returns, up_probabilities = self._fit_predict(
                x_train,
                y_return,
                y_direction,
                x_validation,
            )
            for row_index, row in enumerate(validation.itertuples()):
                predictions.append(
                    _FoldPrediction(
                        actual_return=float(row.target_return),
                        predicted_return=float(predicted_returns[row_index]),
                        up_probability=float(up_probabilities[row_index]),
                        train_end_at=self._timestamp(train.index[-1]),
                        validation_start_at=self._timestamp(validation.index[0]),
                    )
                )
        return predictions

    @staticmethod
    def _fit_predict(
        x_train: np.ndarray,
        y_return: np.ndarray,
        y_direction: np.ndarray,
        x_predict: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        mean = x_train.mean(axis=0)
        std = x_train.std(axis=0)
        std[std < 1e-12] = 1.0
        train = (x_train - mean) / std
        predict = (x_predict - mean) / std

        design_train = np.column_stack((np.ones(len(train)), train))
        design_predict = np.column_stack((np.ones(len(predict)), predict))
        penalty = np.eye(design_train.shape[1])
        penalty[0, 0] = 0.0
        ridge_weights = np.linalg.pinv(design_train.T @ design_train + penalty) @ design_train.T @ y_return
        predicted_returns = design_predict @ ridge_weights

        probabilities = BtcShadowForecastService._logistic_probability(
            design_train,
            y_direction,
            design_predict,
        )
        return np.asarray(predicted_returns), np.asarray(probabilities)

    @staticmethod
    def _logistic_probability(
        design_train: np.ndarray,
        labels: np.ndarray,
        design_predict: np.ndarray,
    ) -> np.ndarray:
        base_rate = float(labels.mean())
        if base_rate <= 0.0 or base_rate >= 1.0:
            return np.full(len(design_predict), base_rate, dtype=float)

        weights = np.zeros(design_train.shape[1], dtype=float)
        weights[0] = np.log(base_rate / (1.0 - base_rate))
        penalty = np.eye(design_train.shape[1]) * 0.1
        penalty[0, 0] = 0.0
        for _ in range(30):
            probabilities = BtcShadowForecastService._sigmoid(design_train @ weights)
            gradient = design_train.T @ (probabilities - labels) + penalty @ weights
            curvature = (design_train.T * (probabilities * (1.0 - probabilities))) @ design_train + penalty
            update = np.linalg.pinv(curvature) @ gradient
            weights -= update
            if float(np.max(np.abs(update))) < 1e-6:
                break
        return BtcShadowForecastService._sigmoid(design_predict @ weights)

    @staticmethod
    def _sigmoid(values: np.ndarray) -> np.ndarray:
        return 1.0 / (1.0 + np.exp(-np.clip(values, -35.0, 35.0)))

    def _feature_frame(self, bars: pd.DataFrame) -> tuple[pd.DataFrame, int]:
        required = {"date", "open", "high", "low", "close", "volume"}
        if not required.issubset(bars.columns):
            return pd.DataFrame(), 0

        frame = bars.loc[:, list(required)].copy()
        frame["date"] = pd.to_datetime(frame["date"], utc=True, errors="coerce")
        for column in ("open", "high", "low", "close", "volume"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame.dropna(subset=["date", "open", "high", "low", "close"]).sort_values("date")
        frame = frame[frame["close"] > 0].drop_duplicates("date", keep="last")
        frame = self._closed_only(frame, bars.attrs.get("fetched_at"))
        if frame.empty:
            return pd.DataFrame(), 0
        frame = frame.set_index("date")

        returns = frame["close"].pct_change()
        features = pd.DataFrame(index=frame.index)
        for periods in (1, 2, 3, 6, 12, 24):
            features[f"feature_return_lag_{periods}"] = returns.shift(periods - 1)
        for periods in (3, 6, 12, 24):
            features[f"feature_return_mean_{periods}"] = returns.rolling(periods).mean()
            features[f"feature_return_vol_{periods}"] = returns.rolling(periods).std(ddof=0)
        volume_mean = frame["volume"].rolling(24).mean()
        volume_std = frame["volume"].rolling(24).std(ddof=0).replace(0, np.nan)
        features["feature_volume_zscore_24"] = (frame["volume"] - volume_mean) / volume_std
        features["feature_body_pct"] = (frame["close"] - frame["open"]) / frame["open"].replace(0, np.nan)
        features["feature_range_pct"] = (frame["high"] - frame["low"]) / frame["close"]
        features["feature_close_to_ema_24"] = frame["close"] / frame["close"].ewm(span=24, adjust=False).mean() - 1.0
        features["target_return"] = frame["close"].shift(-1) / frame["close"] - 1.0
        return features.replace([np.inf, -np.inf], np.nan), int(len(frame))

    @staticmethod
    def _closed_only(frame: pd.DataFrame, fetched_at: Any) -> pd.DataFrame:
        snapshot = pd.to_datetime(fetched_at, utc=True, errors="coerce")
        if pd.isna(snapshot):
            snapshot = pd.Timestamp(datetime.now(timezone.utc))
        return frame.loc[frame["date"] + pd.Timedelta(hours=1) <= snapshot].copy()

    def _walk_forward_summary(self, predictions: list[_FoldPrediction]) -> Dict[str, Any]:
        actual = np.array([item.actual_return for item in predictions], dtype=float)
        predicted = np.array([item.predicted_return for item in predictions], dtype=float)
        probability = np.array([item.up_probability for item in predictions], dtype=float)
        direction = (actual > 0).astype(float)
        correct = (predicted >= 0) == (actual >= 0)
        folds: Dict[tuple[str, str], int] = {}
        for item in predictions:
            key = (item.train_end_at, item.validation_start_at)
            folds[key] = folds.get(key, 0) + 1
        return {
            "scheme": "expanding_walk_forward",
            "fold_count": len(folds),
            "validation_bars_per_fold": self.validation_bars,
            "out_of_fold_samples": int(len(predictions)),
            "return_mae_pct": round(float(np.mean(np.abs(predicted - actual)) * 100), 4),
            "directional_accuracy": round(float(np.mean(correct)), 4),
            "brier_score": round(float(np.mean((probability - direction) ** 2)), 6),
            "folds": [
                {
                    "train_end_at": train_end_at,
                    "validation_start_at": validation_start_at,
                    "out_of_fold_samples": samples,
                    "scaler_fit_scope": "train_only",
                }
                for (train_end_at, validation_start_at), samples in folds.items()
            ],
        }

    @staticmethod
    def _timestamp(value: Any) -> str:
        timestamp = pd.Timestamp(value)
        return timestamp.isoformat()

    @staticmethod
    def _unavailable(base: Dict[str, Any], reason: str, source_closed_bar_count: int = 0) -> Dict[str, Any]:
        return {
            **base,
            "data_quality": "unavailable",
            "reason": reason,
            "source_closed_bar_count": source_closed_bar_count,
            "forecast": None,
            "walk_forward": None,
        }
