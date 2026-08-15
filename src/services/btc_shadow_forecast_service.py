# -*- coding: utf-8 -*-
"""Leakage-safe, observation-only BTC hourly return forecasts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


MODEL_VERSION = "btc-hourly-shadow-cost-aware-wf-v3"
DEFAULT_LOOKBACK_DAYS = 2500
DEFAULT_MIN_TRAIN_BARS = 336
DEFAULT_FOLDS = 12
DEFAULT_VALIDATION_BARS = 168
DEFAULT_CURVE_HORIZON_HOURS = 24
DEFAULT_CONFIDENCE_THRESHOLD = 0.58
DEFAULT_ROUND_TRIP_COST_BPS = 14.0
DEFAULT_PRIMARY_HORIZON_HOURS = 4
TRADE_CLASSES = np.array([-1, 0, 1], dtype=int)
TRADE_CLASS_NAMES = {-1: "down", 0: "no_signal", 1: "up"}


@dataclass(frozen=True)
class _FoldPrediction:
    actual_return: float
    predicted_return: float
    up_probability: float
    historical_up_probability: float
    previous_return_probability: float
    train_end_at: str
    validation_start_at: str


@dataclass(frozen=True)
class _TradeFoldPrediction:
    actual_return: float
    actual_class: int
    predicted_action: int
    down_probability: float
    neutral_probability: float
    up_probability: float
    prior_down_probability: float
    prior_neutral_probability: float
    prior_up_probability: float
    selected_model: str
    calibration_weight: float
    train_end_at: str
    validation_start_at: str


class BtcShadowForecastService:
    """Build direct multi-horizon BTC forecasts without affecting decisions.

    The service intentionally uses small numpy models rather than notebook-only
    dependencies. Every validation fold fits its own scaler on prior data only.
    """

    def __init__(
        self,
        *,
        min_train_bars: int = DEFAULT_MIN_TRAIN_BARS,
        folds: int = DEFAULT_FOLDS,
        validation_bars: int = DEFAULT_VALIDATION_BARS,
        curve_horizon_hours: int = DEFAULT_CURVE_HORIZON_HOURS,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        round_trip_cost_bps: float = DEFAULT_ROUND_TRIP_COST_BPS,
        primary_horizon_hours: int = DEFAULT_PRIMARY_HORIZON_HOURS,
    ) -> None:
        self.min_train_bars = max(24, int(min_train_bars))
        self.folds = max(1, int(folds))
        self.validation_bars = max(1, int(validation_bars))
        self.curve_horizon_hours = max(1, int(curve_horizon_hours))
        self.confidence_threshold = min(0.95, max(0.5, float(confidence_threshold)))
        self.round_trip_cost_bps = max(0.0, float(round_trip_cost_bps))
        self.primary_horizon_hours = max(1, int(primary_horizon_hours))

    def build(self, hourly_bars: Optional[pd.DataFrame]) -> Dict[str, Any]:
        base = {
            "model_version": MODEL_VERSION,
            "feature_set_version": "ohlcv-core-v1",
            "primary_model": "walk_forward_logistic_hist_gradient_boosting",
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
                "primary_forecast": None,
                "primary_evaluation": None,
                "curve": None,
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
                "primary_forecast": None,
                "primary_evaluation": None,
                "curve": None,
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
        primary_forecast, primary_evaluation = self._build_primary_forecast(
            features,
            feature_columns,
        )
        curve = self._build_curve(features, feature_columns)
        if not curve:
            return {
                **base,
                "data_quality": "insufficient",
                "reason": "insufficient_labeled_bars_for_forecast_curve",
                "source_closed_bar_count": closed_bar_count,
                "usable_labeled_bar_count": int(len(labeled)),
                "forecast": None,
                "primary_forecast": primary_forecast,
                "primary_evaluation": primary_evaluation,
                "curve": None,
                "walk_forward": self._walk_forward_summary(fold_predictions),
            }

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
            "primary_forecast": primary_forecast,
            "curve": {
                "model": "direct_multi_horizon_ridge_logistic",
                "horizon_hours": self.curve_horizon_hours,
                "base_price": round(float(latest["reference_close"].iloc[0]), 2),
                "points": curve,
                "note": "每个时距直接预测相对基准收盘价；不递归使用前一预测点。",
            },
            "walk_forward": self._walk_forward_summary(fold_predictions),
            "horizon_evaluation": self._evaluate_horizons(features, feature_columns, fold_predictions),
            "primary_evaluation": primary_evaluation,
            "note": "仅用于影子观测与离线校准；不得作为交易方向、入场、仓位或执行触发依据。",
        }

    def _build_primary_forecast(
        self,
        features: pd.DataFrame,
        feature_columns: Iterable[str],
    ) -> tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        columns = list(feature_columns)
        horizon = self.primary_horizon_hours
        labeled = features[columns].copy()
        labeled["target_return"] = (
            features["reference_close"].shift(-horizon) / features["reference_close"] - 1.0
        )
        labeled = labeled.dropna()
        predictions = self._trade_walk_forward(labeled, columns, horizon)
        evaluation = self._trade_evaluation_metrics(predictions, horizon)
        latest = features.dropna(subset=columns).tail(1)
        if len(labeled) < self.min_train_bars or latest.empty:
            return None, evaluation

        selection = self._select_trade_model(labeled, columns)
        labels = self._cost_aware_labels(labeled["target_return"].to_numpy(dtype=float))
        probabilities = self._fit_trade_probabilities(
            selection["model"],
            labeled[columns].to_numpy(dtype=float),
            labels,
            latest[columns].to_numpy(dtype=float),
            float(selection["calibration_weight"]),
        )[0]
        expected_return = float(
            self._ridge_predict(
                labeled[columns].to_numpy(dtype=float),
                labeled["target_return"].to_numpy(dtype=float),
                latest[columns].to_numpy(dtype=float),
            )[0]
        )
        action, directional_probability = self._trade_action(probabilities)
        return {
            "horizon_hours": horizon,
            "target": "cost_aware_up_down_no_signal",
            "cost_threshold_bps": self.round_trip_cost_bps,
            "expected_return_pct": round(expected_return * 100, 4),
            "up_probability": round(float(probabilities[2]), 4),
            "down_probability": round(float(probabilities[0]), 4),
            "no_signal_probability": round(float(probabilities[1]), 4),
            "directional_probability": round(directional_probability, 4),
            "predicted_action": TRADE_CLASS_NAMES[action],
            "selected_model": selection["model"],
            "calibration_weight": round(float(selection["calibration_weight"]), 2),
            "candidate_multiclass_brier": {
                name: round(float(score), 6)
                for name, score in selection["candidate_scores"].items()
            },
            "participates_in_decision": False,
        }, evaluation

    def _trade_walk_forward(
        self,
        labeled: pd.DataFrame,
        feature_columns: Iterable[str],
        horizon: int,
    ) -> list[_TradeFoldPrediction]:
        columns = list(feature_columns)
        starts = self._fold_starts(
            len(labeled),
            minimum_start=self.min_train_bars + horizon,
        )
        predictions: list[_TradeFoldPrediction] = []
        for start in starts:
            train = labeled.iloc[: start - horizon]
            validation = labeled.iloc[start: start + self.validation_bars]
            if len(train) < self.min_train_bars or validation.empty:
                continue
            selection = self._select_trade_model(train, columns)
            train_labels = self._cost_aware_labels(train["target_return"].to_numpy(dtype=float))
            priors = self._class_priors(train_labels)
            probabilities = self._fit_trade_probabilities(
                selection["model"],
                train[columns].to_numpy(dtype=float),
                train_labels,
                validation[columns].to_numpy(dtype=float),
                float(selection["calibration_weight"]),
            )
            actual_returns = validation["target_return"].to_numpy(dtype=float)
            actual_classes = self._cost_aware_labels(actual_returns)
            for row_index, probability in enumerate(probabilities):
                action, _ = self._trade_action(probability)
                predictions.append(
                    _TradeFoldPrediction(
                        actual_return=float(actual_returns[row_index]),
                        actual_class=int(actual_classes[row_index]),
                        predicted_action=action,
                        down_probability=float(probability[0]),
                        neutral_probability=float(probability[1]),
                        up_probability=float(probability[2]),
                        prior_down_probability=float(priors[0]),
                        prior_neutral_probability=float(priors[1]),
                        prior_up_probability=float(priors[2]),
                        selected_model=str(selection["model"]),
                        calibration_weight=float(selection["calibration_weight"]),
                        train_end_at=self._timestamp(train.index[-1]),
                        validation_start_at=self._timestamp(validation.index[0]),
                    )
                )
        return predictions

    def _select_trade_model(
        self,
        train: pd.DataFrame,
        feature_columns: Iterable[str],
    ) -> Dict[str, Any]:
        columns = list(feature_columns)
        maximum_inner_bars = len(train) - self.primary_horizon_hours - 24
        if maximum_inner_bars < 24:
            return {
                "model": "historical_prior",
                "calibration_weight": 0.0,
                "candidate_scores": {"historical_prior": 0.0},
            }
        inner_validation_bars = min(672, max(168, len(train) // 5))
        inner_validation_bars = min(inner_validation_bars, maximum_inner_bars)
        fit_end = len(train) - inner_validation_bars - self.primary_horizon_hours
        fit = train.iloc[:fit_end]
        calibration = train.iloc[-inner_validation_bars:]
        fit_labels = self._cost_aware_labels(fit["target_return"].to_numpy(dtype=float))
        calibration_labels = self._cost_aware_labels(
            calibration["target_return"].to_numpy(dtype=float)
        )
        priors = self._class_priors(fit_labels)
        if len(np.unique(fit_labels)) < 2:
            return {
                "model": "historical_prior",
                "calibration_weight": 0.0,
                "candidate_scores": {
                    "historical_prior": self._multiclass_brier(
                        np.tile(priors, (len(calibration_labels), 1)),
                        calibration_labels,
                    )
                },
            }

        candidate_scores: Dict[str, float] = {}
        candidate_weights: Dict[str, float] = {}
        x_fit = fit[columns].to_numpy(dtype=float)
        x_calibration = calibration[columns].to_numpy(dtype=float)
        for model_name in ("logistic", "hist_gradient_boosting"):
            try:
                raw_probabilities = self._raw_trade_probabilities(
                    model_name,
                    x_fit,
                    fit_labels,
                    x_calibration,
                )
            except ValueError:
                continue
            best_score = float("inf")
            best_weight = 0.0
            for weight in (0.0, 0.25, 0.5, 0.75, 1.0):
                calibrated = weight * raw_probabilities + (1.0 - weight) * priors
                score = self._multiclass_brier(calibrated, calibration_labels)
                if score < best_score:
                    best_score = score
                    best_weight = weight
            candidate_scores[model_name] = best_score
            candidate_weights[model_name] = best_weight

        if not candidate_scores:
            return {
                "model": "historical_prior",
                "calibration_weight": 0.0,
                "candidate_scores": {"historical_prior": 0.0},
            }
        selected_model = min(candidate_scores, key=candidate_scores.get)
        return {
            "model": selected_model,
            "calibration_weight": candidate_weights[selected_model],
            "candidate_scores": candidate_scores,
        }

    def _fit_trade_probabilities(
        self,
        model_name: str,
        x_train: np.ndarray,
        labels: np.ndarray,
        x_predict: np.ndarray,
        calibration_weight: float,
    ) -> np.ndarray:
        priors = self._class_priors(labels)
        if model_name == "historical_prior" or len(np.unique(labels)) < 2:
            return np.tile(priors, (len(x_predict), 1))
        raw = self._raw_trade_probabilities(model_name, x_train, labels, x_predict)
        return calibration_weight * raw + (1.0 - calibration_weight) * priors

    @staticmethod
    def _raw_trade_probabilities(
        model_name: str,
        x_train: np.ndarray,
        labels: np.ndarray,
        x_predict: np.ndarray,
    ) -> np.ndarray:
        if model_name == "logistic":
            estimator = make_pipeline(
                StandardScaler(),
                LogisticRegression(C=0.25, max_iter=300),
            )
        elif model_name == "hist_gradient_boosting":
            estimator = HistGradientBoostingClassifier(
                max_iter=80,
                learning_rate=0.05,
                max_leaf_nodes=15,
                min_samples_leaf=80,
                l2_regularization=1.0,
                random_state=42,
            )
        else:
            raise ValueError(f"unsupported trade model: {model_name}")
        estimator.fit(x_train, labels)
        raw = estimator.predict_proba(x_predict)
        aligned = np.zeros((len(x_predict), len(TRADE_CLASSES)), dtype=float)
        for source_index, class_value in enumerate(estimator.classes_):
            target_index = int(np.where(TRADE_CLASSES == int(class_value))[0][0])
            aligned[:, target_index] = raw[:, source_index]
        return aligned

    def _trade_action(self, probabilities: np.ndarray) -> tuple[int, float]:
        directional_total = float(probabilities[0] + probabilities[2])
        if directional_total <= 0.0:
            return 0, 0.5
        conditional_up = float(probabilities[2] / directional_total)
        directional_probability = max(conditional_up, 1.0 - conditional_up)
        if float(probabilities[1]) >= 0.35 or directional_probability < self.confidence_threshold:
            return 0, directional_probability
        return (1 if conditional_up >= 0.5 else -1), directional_probability

    def _trade_evaluation_metrics(
        self,
        predictions: list[_TradeFoldPrediction],
        horizon: int,
    ) -> Dict[str, Any]:
        if not predictions:
            return {"data_quality": "insufficient", "out_of_fold_samples": 0}
        actual_return = np.array([item.actual_return for item in predictions], dtype=float)
        actual_class = np.array([item.actual_class for item in predictions], dtype=int)
        predicted_action = np.array([item.predicted_action for item in predictions], dtype=int)
        probability = np.array(
            [
                (item.down_probability, item.neutral_probability, item.up_probability)
                for item in predictions
            ],
            dtype=float,
        )
        prior_probability = np.array(
            [
                (
                    item.prior_down_probability,
                    item.prior_neutral_probability,
                    item.prior_up_probability,
                )
                for item in predictions
            ],
            dtype=float,
        )
        selected = predicted_action != 0
        signed_return = predicted_action[selected] * actual_return[selected]
        net_return = signed_return - self.round_trip_cost_bps / 10_000.0
        fold_keys: list[tuple[str, str]] = []
        for item in predictions:
            key = (item.train_end_at, item.validation_start_at)
            if key not in fold_keys:
                fold_keys.append(key)
        folds = []
        positive_fold_count = 0
        model_counts: Dict[str, int] = {}
        for train_end_at, validation_start_at in fold_keys:
            fold_items = [
                item
                for item in predictions
                if item.train_end_at == train_end_at
                and item.validation_start_at == validation_start_at
            ]
            model_name = fold_items[0].selected_model
            model_counts[model_name] = model_counts.get(model_name, 0) + 1
            fold_actions = np.array([item.predicted_action for item in fold_items], dtype=int)
            fold_returns = np.array([item.actual_return for item in fold_items], dtype=float)
            fold_selected = fold_actions != 0
            fold_net = (
                fold_actions[fold_selected] * fold_returns[fold_selected]
                - self.round_trip_cost_bps / 10_000.0
            )
            fold_net_mean = float(np.mean(fold_net)) if len(fold_net) else None
            if fold_net_mean is not None and fold_net_mean > 0.0:
                positive_fold_count += 1
            folds.append(
                {
                    "train_end_at": train_end_at,
                    "validation_start_at": validation_start_at,
                    "selected_model": model_name,
                    "calibration_weight": round(fold_items[0].calibration_weight, 2),
                    "out_of_fold_samples": len(fold_items),
                    "signal_samples": int(fold_selected.sum()),
                    "net_mean_return_pct_after_cost": (
                        round(fold_net_mean * 100, 4) if fold_net_mean is not None else None
                    ),
                    "purged_horizon_bars": horizon,
                    "inner_purged_horizon_bars": horizon,
                    "model_selection_scope": "train_inner_tail_only",
                }
            )
        multiclass_brier = self._multiclass_brier(probability, actual_class)
        baseline_brier = self._multiclass_brier(prior_probability, actual_class)
        positive_fold_rate = positive_fold_count / len(folds)
        recent_folds_positive = bool(folds) and all(
            fold["net_mean_return_pct_after_cost"] is not None
            and fold["net_mean_return_pct_after_cost"] > 0.0
            for fold in folds[-3:]
        )
        signal_samples = int(selected.sum())
        net_mean = float(np.mean(net_return)) if signal_samples else None
        eligible = bool(
            len(predictions) >= 1000
            and signal_samples >= 200
            and net_mean is not None
            and net_mean > 0.0
            and positive_fold_rate >= 2.0 / 3.0
            and recent_folds_positive
            and multiclass_brier < baseline_brier
        )
        return {
            "data_quality": "available",
            "scheme": "purged_expanding_walk_forward_with_inner_model_selection",
            "horizon_hours": horizon,
            "cost_threshold_bps": self.round_trip_cost_bps,
            "classes": ["down", "no_signal", "up"],
            "out_of_fold_samples": len(predictions),
            "multiclass_brier_score": round(multiclass_brier, 6),
            "historical_class_rate_brier_score": round(baseline_brier, 6),
            "three_class_accuracy": round(float(np.mean(predicted_action == actual_class)), 4),
            "signal_samples": signal_samples,
            "signal_coverage": round(float(np.mean(selected)), 4),
            "directional_accuracy_on_signals": (
                round(float(np.mean(signed_return > 0.0)), 4) if signal_samples else None
            ),
            "net_mean_return_pct_after_cost": (
                round(net_mean * 100, 4) if net_mean is not None else None
            ),
            "profitable_after_cost_rate": (
                round(float(np.mean(net_return > 0.0)), 4) if signal_samples else None
            ),
            "positive_fold_rate": round(positive_fold_rate, 4),
            "recent_three_folds_positive": recent_folds_positive,
            "selected_model_fold_counts": model_counts,
            "eligible_for_promotion": eligible,
            "folds": folds,
        }

    def _cost_aware_labels(self, returns: np.ndarray) -> np.ndarray:
        threshold = self.round_trip_cost_bps / 10_000.0
        return np.where(returns > threshold, 1, np.where(returns < -threshold, -1, 0))

    @staticmethod
    def _class_priors(labels: np.ndarray) -> np.ndarray:
        return np.array([float(np.mean(labels == value)) for value in TRADE_CLASSES])

    @staticmethod
    def _multiclass_brier(probabilities: np.ndarray, labels: np.ndarray) -> float:
        observed = (labels[:, None] == TRADE_CLASSES).astype(float)
        return float(np.mean(np.sum((probabilities - observed) ** 2, axis=1)))

    def _walk_forward(
        self,
        labeled: pd.DataFrame,
        feature_columns: Iterable[str],
    ) -> list[_FoldPrediction]:
        starts = self._fold_starts(len(labeled))
        if not starts:
            return []

        predictions: list[_FoldPrediction] = []
        columns = list(feature_columns)
        for start in starts:
            stop = start + self.validation_bars
            train = labeled.iloc[:start]
            validation = labeled.iloc[start:stop]
            if len(train) < self.min_train_bars or validation.empty:
                continue

            x_train = train[columns].to_numpy(dtype=float)
            y_return = train["target_return"].to_numpy(dtype=float)
            y_direction = (y_return > 0).astype(float)
            historical_up_probability = float(y_direction.mean())
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
                        historical_up_probability=historical_up_probability,
                        previous_return_probability=(
                            1.0 if float(row.feature_return_lag_1) >= 0 else 0.0
                        ),
                        train_end_at=self._timestamp(train.index[-1]),
                        validation_start_at=self._timestamp(validation.index[0]),
                    )
                )
        return predictions

    def _fold_starts(
        self,
        labeled_count: int,
        *,
        minimum_start: Optional[int] = None,
    ) -> list[int]:
        first_start = self.min_train_bars if minimum_start is None else int(minimum_start)
        last_start = labeled_count - self.validation_bars
        if last_start < first_start:
            return []
        available_starts = last_start - first_start + 1
        fold_count = min(self.folds, available_starts)
        starts = np.linspace(
            first_start,
            last_start,
            num=fold_count,
            dtype=int,
        )
        return list(dict.fromkeys(int(start) for start in starts))

    def _evaluate_horizons(
        self,
        features: pd.DataFrame,
        feature_columns: Iterable[str],
        one_hour_predictions: list[_FoldPrediction],
    ) -> Dict[str, Dict[str, Any]]:
        columns = list(feature_columns)
        evaluations = {"1h": self._evaluation_metrics(one_hour_predictions)}
        close = features["reference_close"]
        for horizon in (4, 12, 24):
            labeled = features[columns].copy()
            labeled["target_return"] = close.shift(-horizon) / close - 1.0
            predictions = self._walk_forward(labeled.dropna(), columns)
            evaluations[f"{horizon}h"] = self._evaluation_metrics(predictions)
        return evaluations

    def _evaluation_metrics(self, predictions: list[_FoldPrediction]) -> Dict[str, Any]:
        if not predictions:
            return {"data_quality": "insufficient", "out_of_fold_samples": 0}
        summary = self._walk_forward_summary(predictions)
        return {
            "data_quality": "available",
            "out_of_fold_samples": summary["out_of_fold_samples"],
            "return_mae_pct": summary["return_mae_pct"],
            "directional_accuracy": summary["directional_accuracy"],
            "brier_score": summary["brier_score"],
            "baselines": summary["baselines"],
            "confidence_slices": summary["confidence_slices"],
        }

    def _build_curve(
        self,
        features: pd.DataFrame,
        feature_columns: Iterable[str],
    ) -> list[Dict[str, Any]]:
        """Fit one direct return/probability model for each future horizon."""
        columns = list(feature_columns)
        latest = features.dropna(subset=columns).tail(1)
        if latest.empty:
            return []
        latest_features = latest[columns].to_numpy(dtype=float)
        base_price = float(latest["reference_close"].iloc[0])
        close = features["reference_close"]
        points: list[Dict[str, Any]] = []
        for horizon in range(1, self.curve_horizon_hours + 1):
            target = close.shift(-horizon) / close - 1.0
            training = features[columns].copy()
            training["target"] = target
            training = training.dropna()
            if len(training) < self.min_train_bars:
                break
            x_train = training[columns].to_numpy(dtype=float)
            y_return = training["target"].to_numpy(dtype=float)
            y_direction = (y_return > 0).astype(float)
            predicted_return, up_probability = self._fit_predict(
                x_train,
                y_return,
                y_direction,
                latest_features,
            )
            predicted_return_value = float(predicted_return[0])
            up_probability_value = float(up_probability[0])
            points.append(
                {
                    "offset_hours": horizon,
                    "predicted_return_pct": round(predicted_return_value * 100, 4),
                    "predicted_price": round(base_price * (1.0 + predicted_return_value), 2),
                    "up_probability": round(up_probability_value, 4),
                    "down_probability": round(1.0 - up_probability_value, 4),
                    "predicted_direction": "up" if up_probability_value >= 0.5 else "down",
                    "training_bars": int(len(training)),
                }
            )
        return points

    @staticmethod
    def _fit_predict(
        x_train: np.ndarray,
        y_return: np.ndarray,
        y_direction: np.ndarray,
        x_predict: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        mean, std, design_train, design_predict = BtcShadowForecastService._scaled_design(
            x_train,
            x_predict,
        )
        del mean, std
        predicted_returns = BtcShadowForecastService._ridge_from_design(
            design_train,
            y_return,
            design_predict,
        )

        probabilities = BtcShadowForecastService._logistic_probability(
            design_train,
            y_direction,
            design_predict,
        )
        return np.asarray(predicted_returns), np.asarray(probabilities)

    @staticmethod
    def _ridge_predict(
        x_train: np.ndarray,
        y_return: np.ndarray,
        x_predict: np.ndarray,
    ) -> np.ndarray:
        _, _, design_train, design_predict = BtcShadowForecastService._scaled_design(
            x_train,
            x_predict,
        )
        return BtcShadowForecastService._ridge_from_design(
            design_train,
            y_return,
            design_predict,
        )

    @staticmethod
    def _scaled_design(
        x_train: np.ndarray,
        x_predict: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        mean = x_train.mean(axis=0)
        std = x_train.std(axis=0)
        std[std < 1e-12] = 1.0
        train = (x_train - mean) / std
        predict = (x_predict - mean) / std
        design_train = np.column_stack((np.ones(len(train)), train))
        design_predict = np.column_stack((np.ones(len(predict)), predict))
        return mean, std, design_train, design_predict

    @staticmethod
    def _ridge_from_design(
        design_train: np.ndarray,
        y_return: np.ndarray,
        design_predict: np.ndarray,
    ) -> np.ndarray:
        penalty = np.eye(design_train.shape[1])
        penalty[0, 0] = 0.0
        weights = (
            np.linalg.pinv(design_train.T @ design_train + penalty)
            @ design_train.T
            @ y_return
        )
        return np.asarray(design_predict @ weights)

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
        required = ("date", "open", "high", "low", "close", "volume")
        if not set(required).issubset(bars.columns):
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
        features["reference_close"] = frame["close"]
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
        historical_probability = np.array(
            [item.historical_up_probability for item in predictions],
            dtype=float,
        )
        previous_return_probability = np.array(
            [item.previous_return_probability for item in predictions],
            dtype=float,
        )
        direction = (actual > 0).astype(float)
        correct = (predicted >= 0) == (actual >= 0)
        folds: Dict[tuple[str, str], int] = {}
        for item in predictions:
            key = (item.train_end_at, item.validation_start_at)
            folds[key] = folds.get(key, 0) + 1
        return {
            "scheme": "expanding_walk_forward",
            "origin_selection": "evenly_spaced_historical",
            "fold_count": len(folds),
            "validation_bars_per_fold": self.validation_bars,
            "out_of_fold_samples": int(len(predictions)),
            "return_mae_pct": round(float(np.mean(np.abs(predicted - actual)) * 100), 4),
            "directional_accuracy": round(float(np.mean(correct)), 4),
            "brier_score": round(float(np.mean((probability - direction) ** 2)), 6),
            "baselines": {
                "always_up_directional_accuracy": round(float(np.mean(direction)), 4),
                "previous_return_directional_accuracy": round(
                    float(np.mean((previous_return_probability >= 0.5) == (direction >= 0.5))),
                    4,
                ),
                "historical_up_rate_brier_score": round(
                    float(np.mean((historical_probability - direction) ** 2)),
                    6,
                ),
                "constant_0_5_brier_score": round(
                    float(np.mean((0.5 - direction) ** 2)),
                    6,
                ),
            },
            "confidence_slices": self._confidence_slices(probability, direction, actual),
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

    def _confidence_slices(
        self,
        probability: np.ndarray,
        direction: np.ndarray,
        actual_return: np.ndarray,
    ) -> Dict[str, Any]:
        selected = (probability >= self.confidence_threshold) | (
            probability <= 1.0 - self.confidence_threshold
        )
        sample_count = int(selected.sum())
        result: Dict[str, Any] = {
            "high_confidence": {
                "probability_threshold": self.confidence_threshold,
                "out_of_fold_samples": sample_count,
                "coverage": round(float(sample_count / len(probability)), 4),
                "round_trip_cost_bps": self.round_trip_cost_bps,
            }
        }
        if sample_count:
            selected_probability = probability[selected]
            selected_direction = direction[selected]
            predicted_direction = selected_probability >= 0.5
            selected_actual_return = actual_return[selected]
            signed_return = np.where(predicted_direction, selected_actual_return, -selected_actual_return)
            net_return = signed_return - self.round_trip_cost_bps / 10_000.0
            result["high_confidence"].update(
                {
                    "directional_accuracy": round(
                        float(np.mean(predicted_direction == (selected_direction >= 0.5))),
                        4,
                    ),
                    "brier_score": round(
                        float(np.mean((selected_probability - selected_direction) ** 2)),
                        6,
                    ),
                    "gross_mean_return_pct": round(float(np.mean(signed_return) * 100), 4),
                    "net_mean_return_pct_after_cost": round(float(np.mean(net_return) * 100), 4),
                    "profitable_after_cost_rate": round(float(np.mean(net_return > 0)), 4),
                }
            )
        return result

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
            "primary_forecast": None,
            "primary_evaluation": None,
            "curve": None,
            "walk_forward": None,
        }
