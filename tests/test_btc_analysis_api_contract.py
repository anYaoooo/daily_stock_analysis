"""Active BTC-only contract tests for the analysis endpoint."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from api.v1.endpoints.analysis import trigger_analysis
from api.v1.schemas.analysis import AnalyzeRequest


def _request(**overrides):
    payload = {
        "stock_code": "BTCUSDT",
        "stock_codes": None,
        "stock_name": None,
        "original_query": "BTCUSDT",
        "selection_source": "manual",
        "report_type": "detailed",
        "force_refresh": False,
        "async_mode": True,
        "notify": True,
        "analysis_phase": "auto",
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def test_trigger_analysis_normalizes_btc_alias_before_queueing() -> None:
    queue = MagicMock()
    queue.submit_tasks_batch.return_value = ([], [])

    with patch("api.v1.endpoints.analysis.get_task_queue", return_value=queue):
        response = trigger_analysis(request=_request(), config=SimpleNamespace())

    assert response.status_code == 202
    queue.submit_tasks_batch.assert_called_once_with(
        stock_codes=["BTC"],
        stock_name="Bitcoin",
        original_query="BTCUSDT",
        selection_source="manual",
        report_type="detailed",
        analysis_phase="auto",
        force_refresh=False,
        notify=True,
    )


@pytest.mark.parametrize("alias", ["BTC", "BTC-USD", "BTC/USD", "BTCUSD"])
def test_trigger_analysis_accepts_all_btc_aliases(alias: str) -> None:
    queue = MagicMock()
    queue.submit_tasks_batch.return_value = ([], [])

    with patch("api.v1.endpoints.analysis.get_task_queue", return_value=queue):
        response = trigger_analysis(
            request=_request(stock_code=alias, original_query=alias),
            config=SimpleNamespace(),
        )

    assert response.status_code == 202
    assert queue.submit_tasks_batch.call_args.kwargs["stock_codes"] == ["BTC"]


def test_trigger_analysis_rejects_non_btc_without_name_resolution() -> None:
    queue = MagicMock()

    with patch("api.v1.endpoints.analysis.get_task_queue", return_value=queue):
        with pytest.raises(Exception) as exc_info:
            trigger_analysis(request=_request(stock_code="AAPL"), config=SimpleNamespace())

    exc = exc_info.value
    assert exc.status_code == 400
    assert "仅支持 BTC" in exc.detail["message"]
    queue.submit_tasks_batch.assert_not_called()


def test_trigger_analysis_rejects_non_btc_in_compatibility_batch() -> None:
    queue = MagicMock()

    with patch("api.v1.endpoints.analysis.get_task_queue", return_value=queue):
        with pytest.raises(Exception) as exc_info:
            trigger_analysis(
                request=_request(stock_code=None, stock_codes=["BTC", "600519"]),
                config=SimpleNamespace(),
            )

    exc = exc_info.value
    assert exc.status_code == 400
    assert "仅支持 BTC" in exc.detail["message"]
    queue.submit_tasks_batch.assert_not_called()


def test_analyze_request_openapi_example_is_btc() -> None:
    schema = AnalyzeRequest.model_json_schema()
    assert schema["properties"]["stock_code"]["example"] == "BTC"
    assert schema["example"]["stock_name"] == "Bitcoin"

