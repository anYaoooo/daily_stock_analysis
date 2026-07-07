# -*- coding: utf-8 -*-
"""Tests for Scheduler background task support."""

from datetime import datetime
import sys
import unittest
from unittest.mock import MagicMock, patch


class _FakeJob:
    def __init__(self, schedule_module):
        self._schedule_module = schedule_module
        self.next_run = datetime(2026, 1, 1, 18, 0, 0)
        self.at_time = None
        self.period = None

    @property
    def day(self):
        self.period = "day"
        return self

    @property
    def hour(self):
        self.period = "hour"
        return self

    def at(self, value):
        self.at_time = value
        if value.startswith(":"):
            minute = int(value[1:])
            self.next_run = datetime(2026, 1, 1, 18, minute, 0)
        else:
            hour, minute = [int(part) for part in value.split(":")]
            self.next_run = datetime(2026, 1, 1, hour, minute, 0)
        return self

    def do(self, fn):
        self.job_func = fn
        self._schedule_module.jobs.append(self)
        return self


class _FakeScheduleModule:
    def __init__(self):
        self.jobs = []

    def every(self):
        return _FakeJob(self)

    def get_jobs(self):
        return list(self.jobs)

    def run_pending(self):
        return None

    def cancel_job(self, job):
        self.jobs.remove(job)


class SchedulerBackgroundTaskTestCase(unittest.TestCase):
    def test_background_task_runs_when_interval_elapsed(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler(schedule_time="18:00")
            calls = []
            fake_thread = MagicMock()
            fake_thread.is_alive.return_value = False

            def _make_thread(target=None, **kwargs):
                fake_thread.start.side_effect = target
                return fake_thread

            with patch("src.scheduler.threading.Thread", side_effect=_make_thread):
                scheduler.add_background_task(lambda: calls.append("ran"), interval_seconds=1, run_immediately=True, name="test")

        self.assertEqual(calls, ["ran"])

    def test_background_task_waits_for_interval(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler(schedule_time="18:00")
            calls = []
            scheduler.add_background_task(lambda: calls.append("ran"), interval_seconds=60, run_immediately=False, name="test")

            with patch("src.scheduler.time.time", return_value=scheduler._background_tasks[0]["last_run"] + 10):
                scheduler._run_background_tasks()

        self.assertEqual(calls, [])

    def test_background_task_runs_once_per_hour_at_configured_minute(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler(schedule_time="18:00")
            calls = []
            fake_thread = MagicMock()
            fake_thread.is_alive.return_value = False

            def _make_thread(target=None, **kwargs):
                fake_thread.start.side_effect = target
                return fake_thread

            scheduler.add_background_task(
                lambda: calls.append("ran"),
                interval_seconds=60 * 60,
                hourly_at_minute=5,
                run_immediately=False,
                name="hourly",
            )

            with patch("src.scheduler.threading.Thread", side_effect=_make_thread), \
                 patch("src.scheduler.datetime") as datetime_mock:
                datetime_mock.now.return_value = datetime(2026, 1, 1, 1, 4, 30)
                scheduler._run_background_tasks()

                datetime_mock.now.return_value = datetime(2026, 1, 1, 1, 5, 0)
                scheduler._run_background_tasks()
                scheduler._run_background_tasks()

                datetime_mock.now.return_value = datetime(2026, 1, 1, 2, 5, 0)
                scheduler._run_background_tasks()

        self.assertEqual(calls, ["ran", "ran"])

    def test_background_task_respects_hourly_interval_hours(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler(schedule_time="18:00")
            calls = []
            fake_thread = MagicMock()
            fake_thread.is_alive.return_value = False

            def _make_thread(target=None, **kwargs):
                fake_thread.start.side_effect = target
                return fake_thread

            scheduler.add_background_task(
                lambda: calls.append("ran"),
                interval_seconds=60 * 60,
                hourly_at_minute=5,
                hourly_interval_hours=4,
                run_immediately=False,
                name="hourly",
            )

            with patch("src.scheduler.threading.Thread", side_effect=_make_thread), \
                 patch("src.scheduler.datetime") as datetime_mock, \
                 patch("src.scheduler.time.time") as time_mock:
                datetime_mock.now.return_value = datetime(2026, 1, 1, 1, 5, 0)
                time_mock.return_value = 1000.0
                scheduler._run_background_tasks()

                datetime_mock.now.return_value = datetime(2026, 1, 1, 2, 5, 0)
                time_mock.return_value = 4600.0
                scheduler._run_background_tasks()

                datetime_mock.now.return_value = datetime(2026, 1, 1, 5, 5, 0)
                time_mock.return_value = 15400.0
                scheduler._run_background_tasks()

        self.assertEqual(calls, ["ran", "ran"])

    def test_background_task_rejects_invalid_hourly_minute(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler(schedule_time="18:00")

            with self.assertRaises(ValueError):
                scheduler.add_background_task(
                    lambda: None,
                    interval_seconds=60 * 60,
                    hourly_at_minute=60,
                    name="hourly",
                )

    def test_run_with_schedule_registers_background_tasks_before_immediate_daily_task(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src import scheduler as scheduler_module

            order = []

            class FakeScheduler:
                def __init__(self, schedule_time="18:00", schedule_time_provider=None, schedule_mode="daily"):
                    order.append(("init", schedule_time))
                    order.append(("provider", callable(schedule_time_provider)))
                    order.append(("mode", schedule_mode))

                def add_background_task(self, **kwargs):
                    order.append((
                        "background",
                        kwargs["name"],
                        kwargs.get("hourly_at_minute"),
                        kwargs.get("hourly_interval_hours"),
                    ))

                def set_daily_task(self, task, run_immediately=True):
                    order.append(("daily", run_immediately))

                def run(self):
                    order.append(("run", None))

            with patch.object(scheduler_module, "Scheduler", FakeScheduler):
                scheduler_module.run_with_schedule(
                    task=lambda: None,
                    run_immediately=True,
                    background_tasks=[{
                        "task": lambda: None,
                        "interval_seconds": 60,
                        "run_immediately": True,
                        "name": "event_monitor",
                        "hourly_at_minute": 5,
                        "hourly_interval_hours": 4,
                    }],
                )

        self.assertEqual(order[:5], [("init", "18:00"), ("provider", False), ("mode", "daily"), ("background", "event_monitor", 5, 4), ("daily", True)])

    def test_scheduler_hourly_mode_registers_top_of_hour_job(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler(schedule_time="18:00", schedule_mode="hourly")
            scheduler.set_daily_task(lambda: None, run_immediately=False)

        self.assertEqual(len(fake_schedule.jobs), 1)
        self.assertEqual(fake_schedule.jobs[0].period, "hour")
        self.assertEqual(fake_schedule.jobs[0].at_time, ":00")

    def test_scheduler_hourly_mode_ignores_schedule_time_provider(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            provider = MagicMock(return_value="09:30")
            scheduler = Scheduler(
                schedule_time="18:00",
                schedule_time_provider=provider,
                schedule_mode="hourly",
            )
            scheduler.set_daily_task(lambda: None, run_immediately=False)
            scheduler._refresh_daily_schedule_if_needed()

        provider.assert_not_called()
        self.assertEqual(len(fake_schedule.jobs), 1)
        self.assertEqual(fake_schedule.jobs[0].period, "hour")
        self.assertEqual(fake_schedule.jobs[0].at_time, ":00")

    def test_scheduler_reloads_daily_job_when_schedule_time_changes(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler(
                schedule_time="18:00",
                schedule_time_provider=lambda: "09:30",
            )
            scheduler.set_daily_task(lambda: None, run_immediately=False)

            self.assertEqual(len(fake_schedule.jobs), 1)
            self.assertEqual(fake_schedule.jobs[0].at_time, "18:00")

            scheduler._refresh_daily_schedule_if_needed()

        self.assertEqual(len(fake_schedule.jobs), 1)
        self.assertEqual(fake_schedule.jobs[0].at_time, "09:30")
        self.assertEqual(scheduler.schedule_time, "09:30")

    def test_scheduler_keeps_existing_daily_job_when_schedule_time_invalid(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler(
                schedule_time="18:00",
                schedule_time_provider=lambda: "25:99",
            )
            scheduler.set_daily_task(lambda: None, run_immediately=False)

            scheduler._refresh_daily_schedule_if_needed()

        self.assertEqual(len(fake_schedule.jobs), 1)
        self.assertEqual(fake_schedule.jobs[0].at_time, "18:00")
        self.assertEqual(scheduler.schedule_time, "18:00")

    def test_scheduler_keeps_current_daily_job_when_schedule_time_provider_fails(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            provider_calls = {"count": 0}

            def provider():
                provider_calls["count"] += 1
                if provider_calls["count"] == 1:
                    return "09:30"
                raise RuntimeError("boom")

            scheduler = Scheduler(
                schedule_time="18:00",
                schedule_time_provider=provider,
            )
            scheduler.set_daily_task(lambda: None, run_immediately=False)

            scheduler._refresh_daily_schedule_if_needed()
            scheduler._refresh_daily_schedule_if_needed()

        self.assertEqual(len(fake_schedule.jobs), 1)
        self.assertEqual(fake_schedule.jobs[0].at_time, "09:30")
        self.assertEqual(scheduler.schedule_time, "09:30")

    def test_scheduler_rejects_invalid_initial_schedule_time(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler(schedule_time="25:99")
            calls = []

            with self.assertRaisesRegex(ValueError, "25:99"):
                scheduler.set_daily_task(lambda: calls.append("ran"), run_immediately=True)

        self.assertEqual(calls, [])
        self.assertEqual(fake_schedule.jobs, [])


if __name__ == "__main__":
    unittest.main()
