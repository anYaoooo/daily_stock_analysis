from types import SimpleNamespace

import numpy as np
import pandas as pd

import src.services.timesfm_forecast_service as timesfm_service
from src.services.timesfm_forecast_service import StockTimesFMForecastService


def _bars(count: int = 40) -> pd.DataFrame:
    dates = pd.date_range("2025-01-01", periods=count, freq="D", tz="UTC")
    return pd.DataFrame({"date": dates, "close": np.linspace(100.0, 120.0, count)})


def test_timesfm_forecast_maps_point_and_quantiles(monkeypatch) -> None:
    class FakeModel:
        def compile(self, config):
            self.config = config

        def forecast(self, *, horizon, inputs):
            assert horizon == 3
            assert len(inputs) == 1
            point = np.array([[121.0, 122.0, 123.0]], dtype=np.float32)
            quantiles = np.zeros((1, 3, 10), dtype=np.float32)
            quantiles[0, :, 1] = [119.0, 120.0, 121.0]
            quantiles[0, :, 5] = [121.0, 122.0, 123.0]
            quantiles[0, :, 9] = [123.0, 124.0, 125.0]
            return point, quantiles

    fake_module = SimpleNamespace(
        TimesFM_2p5_200M_torch=SimpleNamespace(
            from_pretrained=lambda model_id, **kwargs: FakeModel()
        ),
        ForecastConfig=lambda **kwargs: kwargs,
    )
    monkeypatch.setattr(timesfm_service, "import_module", lambda name: fake_module)
    timesfm_service._MODEL_CACHE.clear()

    result = StockTimesFMForecastService(context_length=32, horizon=3).build(_bars(), stock_code="600519")

    assert result["data_quality"] == "available"
    assert result["stock_code"] == "600519"
    assert result["participates_in_decision"] is False
    assert result["forecast_close"] == [121.0, 122.0, 123.0]
    assert result["forecast_quantiles"]["q10"] == [119.0, 120.0, 121.0]
    assert result["expected_return_pct"] == 2.5


def test_timesfm_forecast_is_fail_open_when_dependency_is_missing(monkeypatch) -> None:
    def missing(_name):
        raise ImportError("timesfm is not installed")

    monkeypatch.setattr(timesfm_service, "import_module", missing)
    timesfm_service._MODEL_CACHE.clear()

    result = StockTimesFMForecastService(context_length=32).build(_bars())

    assert result["data_quality"] == "unavailable"
    assert result["reason"] == "timesfm_dependency_missing"
    assert result["participates_in_decision"] is False


def test_timesfm_forecast_reports_insufficient_history() -> None:
    result = StockTimesFMForecastService(context_length=32).build(_bars(10))

    assert result["data_quality"] == "insufficient"
    assert result["reason"] == "insufficient_daily_bars"
    assert result["source_bar_count"] == 10
