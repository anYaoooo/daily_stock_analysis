# -*- coding: utf-8 -*-
"""
===================================
Analysis Integration Tests
===================================

Covers:
- API endpoint /analyze
- Name resolution to code
- Task queue submission
- Metadata persistence (original_query, selection_source)
"""

import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from api.app import create_app
from src.services.task_queue import AnalysisTaskQueue
import src.auth as auth

@pytest.fixture
def client():
    app = create_app()
    return TestClient(app)


@pytest.fixture(autouse=True)
def disable_auth():
    """Keep analysis integration tests independent from local auth env state."""
    auth._auth_enabled = None
    with patch("api.middlewares.auth.is_auth_enabled", return_value=False), \
         patch("src.auth.is_auth_enabled", return_value=False):
        yield
    auth._auth_enabled = None

@pytest.fixture
def mock_task_queue():
    with patch("api.v1.endpoints.analysis.get_task_queue") as mock_get:
        queue = MagicMock(spec=AnalysisTaskQueue)
        mock_get.return_value = queue
        yield queue

class TestAnalysisIntegration:
    """End-to-end integration tests for the analysis flow."""

    def test_trigger_analysis_flow_manual_btc_alias(self, client, mock_task_queue):
        """A supported BTC alias is normalized before task submission."""
        # Setup mock behavior
        mock_task_queue.submit_tasks_batch.return_value = (
            [MagicMock(task_id="test_task_123", stock_code="BTC", analysis_phase="auto")],
            []
        )

        # Trigger analysis with a public BTC alias.
        response = client.post(
            "/api/v1/analysis/analyze",
            json={
                "stock_code": "BTCUSDT",
                "async_mode": True,
                "original_query": "BTCUSDT",
                "selection_source": "manual"
            }
        )

        assert response.status_code == 202
        data = response.json()
        assert data["task_id"] == "test_task_123"
        assert data["status"] == "pending"

        # Verify task queue received the correct resolved code and metadata.
        # Use call_args so this integration test stays focused on analysis flow
        # semantics even if the queue API gains orthogonal optional flags.
        mock_task_queue.submit_tasks_batch.assert_called_once()
        _, kwargs = mock_task_queue.submit_tasks_batch.call_args
        assert kwargs["stock_codes"] == ["BTC"]
        assert kwargs["stock_name"] == "Bitcoin"
        assert kwargs["original_query"] == "BTCUSDT"
        assert kwargs["selection_source"] == "manual"
        assert kwargs["report_type"] == "detailed"
        assert kwargs["analysis_phase"] == "auto"
        assert kwargs["force_refresh"] is False
        assert kwargs["notify"] is True

    def test_trigger_analysis_batch_deduplication(self, client, mock_task_queue):
        """All compatibility-list BTC aliases collapse to one BTC task."""
        mock_task_queue.submit_tasks_batch.return_value = ([], [])

        client.post(
            "/api/v1/analysis/analyze",
            json={
                "stock_codes": ["BTC", "BTCUSDT", "BTC-USD", "BTC/USD", "BTCUSD"],
                "async_mode": True
            }
        )

        # Should only submit once after de-duplication
        mock_task_queue.submit_tasks_batch.assert_called_once()
        args, kwargs = mock_task_queue.submit_tasks_batch.call_args
        assert len(kwargs["stock_codes"]) == 1
        assert kwargs["stock_codes"] == ["BTC"]
        assert kwargs["stock_name"] == "Bitcoin"
        assert kwargs["analysis_phase"] == "auto"

    def test_trigger_analysis_rejects_non_btc_before_queue(self, client, mock_task_queue):
        """Any non-BTC compatibility-list item rejects the whole request."""
        response = client.post(
            "/api/v1/analysis/analyze",
            json={
                "stock_codes": ["BTC", "ETH"],
                "async_mode": True
            }
        )

        assert response.status_code == 400
        assert "当前系统仅支持 BTC" in response.json()["message"]
        mock_task_queue.submit_tasks_batch.assert_not_called()

    def test_trigger_analysis_uses_fixed_btc_metadata_after_alias_deduplication(
        self, client, mock_task_queue
    ):
        """Alias lists still produce one task with fixed BTC display metadata."""
        mock_task_queue.submit_tasks_batch.return_value = ([], [])

        client.post(
            "/api/v1/analysis/analyze",
            json={
                "stock_codes": ["BTCUSDT", "BTC-USD"],
                "stock_name": "untrusted client name",
                "original_query": "BTC aliases",
                "selection_source": "import",
                "async_mode": True
            }
        )

        mock_task_queue.submit_tasks_batch.assert_called_once()
        args, kwargs = mock_task_queue.submit_tasks_batch.call_args
        assert kwargs["stock_codes"] == ["BTC"]
        assert kwargs["stock_name"] == "Bitcoin"
        assert kwargs["original_query"] == "BTC aliases"
        assert kwargs["selection_source"] == "import"
        assert kwargs["analysis_phase"] == "auto"

    def test_trigger_analysis_explicit_analysis_phase(self, client, mock_task_queue):
        """Explicit analysis_phase is passed through to the task queue."""
        mock_task_queue.submit_tasks_batch.return_value = (
            [MagicMock(task_id="test_task_phase", stock_code="BTC", analysis_phase="intraday")],
            []
        )

        response = client.post(
            "/api/v1/analysis/analyze",
            json={
                "stock_code": "BTC-USD",
                "async_mode": True,
                "analysis_phase": "intraday",
            },
        )

        assert response.status_code == 202
        assert response.json()["analysis_phase"] == "intraday"
        mock_task_queue.submit_tasks_batch.assert_called_once()
        _, kwargs = mock_task_queue.submit_tasks_batch.call_args
        assert kwargs["stock_codes"] == ["BTC"]
        assert kwargs["analysis_phase"] == "intraday"
