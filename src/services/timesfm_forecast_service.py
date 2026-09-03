# -*- coding: utf-8 -*-
"""Optional TimesFM 2.5 price forecast adapter.

The adapter is deliberately observation-only.  It exposes a point forecast and
prediction intervals, but never turns them into a trading decision.
"""

from __future__ import annotations

import threading
from datetime import date, datetime
from importlib import import_module
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd


MODEL_VERSION = "timesfm-2.5-200m-pytorch"
_MODEL_CACHE: dict[tuple[str, str, int, int], Any] = {}
_MODEL_LOCK = threading.RLock()


def _as_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


class StockTimesFMForecastService:
    """Build a fail-open TimesFM forecast from closed daily bars."""

    def __init__(
        self,
        *,
        model_id: str = "google/timesfm-2.5-200m-pytorch",
        cache_dir: str = "",
        context_length: int = 512,
        horizon: int = 5,
        batch_size: int = 4,
        device: str = "",
    ) -> None:
        self.model_id = str(model_id).strip() or "google/timesfm-2.5-200m-pytorch"
        self.cache_dir = str(cache_dir).strip()
        self.context_length = max(32, int(context_length))
        self.horizon = max(1, int(horizon))
        self.batch_size = max(1, int(batch_size))
        self.device = str(device).strip()

    def build(
        self,
        bars: Optional[pd.DataFrame],
        *,
        stock_code: str = "",
        as_of: Any = None,
    ) -> Dict[str, Any]:
        base: Dict[str, Any] = {
            "model_version": MODEL_VERSION,
            "model_id": self.model_id,
            "mode": "shadow",
            "target": "daily_close",
            "participates_in_decision": False,
            "stock_code": str(stock_code or ""),
            "context_length": self.context_length,
            "horizon_bars": self.horizon,
        }
        frame = self._normalize_bars(bars, as_of=as_of)
        if frame.empty:
            return self._unavailable(base, "daily_bars_missing")
        if len(frame) < min(self.context_length, 32):
            return {
                **base,
                "data_quality": "insufficient",
                "reason": "insufficient_daily_bars",
                "source_bar_count": int(len(frame)),
                "minimum_required_bars": min(self.context_length, 32),
            }

        values = frame["close"].to_numpy(dtype=np.float32)[-self.context_length :]
        try:
            model = self._load_model()
            point, quantiles = model.forecast(horizon=self.horizon, inputs=[values])
            point_values = np.asarray(point, dtype=np.float64)
            quantile_values = np.asarray(quantiles, dtype=np.float64)
            if point_values.ndim < 2 or point_values.shape[-1] != self.horizon:
                raise ValueError("unexpected point forecast shape")
            forecast = point_values[0].tolist()
            q10 = q50 = q90 = None
            if quantile_values.ndim == 3 and quantile_values.shape[1] == self.horizon:
                # TimesFM 2.5 returns [mean, q10, ..., q50, ..., q90].
                q10 = quantile_values[0, :, 1].tolist()
                q50 = quantile_values[0, :, 5].tolist()
                q90 = quantile_values[0, :, 9].tolist()
            base_close = float(values[-1])
            if not np.isfinite(base_close) or base_close <= 0:
                raise ValueError("latest close is not positive")
            expected_return = (float(forecast[-1]) / base_close) - 1.0
            result = {
                **base,
                "data_quality": "available",
                "source_bar_count": int(len(frame)),
                "forecast_as_of": _as_iso(frame["date"].iloc[-1]),
                "base_close": round(base_close, 8),
                "forecast_close": [round(float(value), 8) for value in forecast],
                "expected_return_pct": round(expected_return * 100.0, 4),
                "forecast_quantiles": {
                    "q10": [round(float(value), 8) for value in q10] if q10 is not None else None,
                    "q50": [round(float(value), 8) for value in q50] if q50 is not None else None,
                    "q90": [round(float(value), 8) for value in q90] if q90 is not None else None,
                },
                "note": "TimesFM 连续价格预测仅用于观察；未转换为方向概率，也不参与交易决策。",
            }
            return result
        except ImportError:
            return self._unavailable(base, "timesfm_dependency_missing")
        except Exception as exc:  # pragma: no cover - runtime/model failures are environment-specific
            return self._unavailable(base, "timesfm_inference_failed", error_type=type(exc).__name__)

    @staticmethod
    def _normalize_bars(bars: Optional[pd.DataFrame], *, as_of: Any = None) -> pd.DataFrame:
        if bars is None or bars.empty or "close" not in bars.columns:
            return pd.DataFrame()
        frame = bars.copy()
        date_column = "date" if "date" in frame.columns else "timestamp" if "timestamp" in frame.columns else None
        if date_column is None:
            return pd.DataFrame()
        frame["date"] = pd.to_datetime(frame[date_column], utc=True, errors="coerce")
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        frame = frame.dropna(subset=["date", "close"]).sort_values("date").drop_duplicates("date", keep="last")
        frame = frame[frame["close"] > 0]
        if as_of is not None:
            cutoff = pd.to_datetime(as_of, utc=True, errors="coerce")
            if not pd.isna(cutoff):
                frame = frame[frame["date"] <= cutoff]
        return frame.reset_index(drop=True)

    def _load_model(self) -> Any:
        key = (self.model_id, self.cache_dir, self.batch_size, self.device)
        with _MODEL_LOCK:
            if key in _MODEL_CACHE:
                return _MODEL_CACHE[key]
            timesfm = import_module("timesfm")
            model_class = getattr(timesfm, "TimesFM_2p5_200M_torch")
            kwargs: Dict[str, Any] = {}
            if self.cache_dir:
                kwargs["cache_dir"] = self.cache_dir
            # Avoid an expensive torch.compile during service startup; the
            # TimesFM decoder is compiled by ``model.compile`` below.
            model = model_class.from_pretrained(self.model_id, torch_compile=False, **kwargs)
            if self.device:
                torch = import_module("torch")
                model_impl = getattr(model, "model", None)
                if model_impl is not None:
                    model_impl.device = torch.device(self.device)
                    model_impl.device_count = 1
                    model_impl.to(model_impl.device)
            model.compile(
                timesfm.ForecastConfig(
                    max_context=self.context_length,
                    max_horizon=self.horizon,
                    normalize_inputs=True,
                    per_core_batch_size=self.batch_size,
                    use_continuous_quantile_head=True,
                    infer_is_positive=True,
                    fix_quantile_crossing=True,
                )
            )
            _MODEL_CACHE[key] = model
            return model

    @staticmethod
    def _unavailable(base: Dict[str, Any], reason: str, *, error_type: Optional[str] = None) -> Dict[str, Any]:
        result = {**base, "data_quality": "unavailable", "reason": reason}
        if error_type:
            result["error_type"] = error_type
        return result


__all__ = ["MODEL_VERSION", "StockTimesFMForecastService"]
