# -*- coding: utf-8 -*-
"""Regression coverage for asynchronous BTC backtest task endpoints."""

from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from api.v1.endpoints.backtest import (
    get_selected_crypto_backtest_task,
    start_selected_crypto_backtests,
)
from api.v1.schemas.backtest import CryptoBacktestSelectedRunRequest
from src.services.task_queue import TaskStatus


class CryptoBacktestTaskApiTestCase(unittest.TestCase):
    @patch("api.v1.endpoints.backtest.CryptoBacktestService")
    @patch("api.v1.endpoints.backtest.get_task_queue")
    def test_selected_backtest_is_accepted_before_running(self, queue_factory, service_cls) -> None:
        queue = MagicMock()
        queue_factory.return_value = queue
        queue.submit_background_task.return_value = SimpleNamespace(
            task_id="backtest-task-1",
            status=TaskStatus.PENDING,
            message="已加入回测队列（2 条记录）",
        )
        service_cls.return_value.run_selected_backtests.return_value = {
            "processed": 2,
            "saved": 2,
            "completed": 2,
            "insufficient": 0,
            "skipped": 0,
            "errors": 0,
        }
        db_manager = MagicMock()

        response = start_selected_crypto_backtests(
            CryptoBacktestSelectedRunRequest(
                analysis_history_ids=[12, 13],
                plan_types=["daily_long"],
                force=True,
            ),
            db_manager=db_manager,
        )

        self.assertEqual(response.task_id, "backtest-task-1")
        self.assertEqual(response.status, "pending")
        run_task = queue.submit_background_task.call_args.args[0]
        self.assertEqual(
            run_task(),
            {
                "processed": 2,
                "saved": 2,
                "completed": 2,
                "insufficient": 0,
                "skipped": 0,
                "errors": 0,
            },
        )
        service_cls.assert_called_once_with(db_manager)
        service_cls.return_value.run_selected_backtests.assert_called_once_with(
            analysis_history_ids=[12, 13],
            plan_types=["daily_long"],
            force=True,
        )

    @patch("api.v1.endpoints.backtest.get_task_queue")
    def test_completed_task_returns_backtest_result(self, queue_factory) -> None:
        queue_factory.return_value.get_task.return_value = SimpleNamespace(
            task_id="backtest-task-2",
            report_type="backtest",
            status=TaskStatus.COMPLETED,
            progress=100,
            message="任务执行完成",
            result={
                "processed": 1,
                "saved": 1,
                "completed": 1,
                "insufficient": 0,
                "skipped": 0,
                "errors": 0,
            },
            error=None,
        )

        response = get_selected_crypto_backtest_task("backtest-task-2")

        self.assertEqual(response.status, "completed")
        self.assertEqual(response.progress, 100)
        self.assertIsNotNone(response.result)
        self.assertEqual(response.result.saved, 1)

    @patch("api.v1.endpoints.backtest.get_task_queue")
    def test_unknown_or_non_backtest_task_is_not_exposed(self, queue_factory) -> None:
        queue_factory.return_value.get_task.return_value = SimpleNamespace(report_type="detailed")

        with self.assertRaises(HTTPException) as ctx:
            get_selected_crypto_backtest_task("analysis-task")

        self.assertEqual(ctx.exception.status_code, 404)

    def test_selected_backtest_rejects_unsupported_plan_type(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            start_selected_crypto_backtests(
                CryptoBacktestSelectedRunRequest(
                    analysis_history_ids=[12],
                    plan_types=["unsupported"],
                ),
                db_manager=MagicMock(),
            )

        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
