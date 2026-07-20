# -*- coding: utf-8 -*-
"""Pytest compatibility hooks."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import sys
import time
import threading
from collections.abc import Awaitable, Callable
from contextvars import copy_context
from functools import wraps
from typing import Any, TypeVar
from warnings import warn

import anyio.to_thread
import fastapi.testclient
import httpx
import pytest
import starlette.testclient
from anyio._backends import _asyncio

T = TypeVar("T")

# These tests cover the pre-BTC-only stock product. Keep the boundary explicit so
# the default suite cannot silently grow new dependencies on retired modules.
LEGACY_STOCK_TEST_FILES = frozenset(
    {
        "test_a_share_fetcher_code_conversion.py",
        "test_akshare_history_timeout.py",
        "test_akshare_realtime_logging.py",
        "test_alphavantage_fetcher.py",
        "test_belong_boards_run_flow.py",
        "test_chip_distribution_manager.py",
        "test_chip_structure_fallback.py",
        "test_data_fetcher_prefetch_stock_names.py",
        "test_data_tools_get_capital_flow.py",
        "test_data_tools_get_stock_info.py",
        "test_efinance_main_indices.py",
        "test_etf_daily_routing.py",
        "test_fetch_tushare_stock_list.py",
        "test_fetcher_source_optimization.py",
        "test_finnhub_fetcher.py",
        "test_fundamental_adapter.py",
        "test_fundamental_context.py",
        "test_generate_index_from_csv.py",
        "test_get_latest_data.py",
        "test_hk_realtime_routing.py",
        "test_hk_stock_name_fallback.py",
        "test_image_stock_extractor_litellm.py",
        "test_longbridge_fetcher.py",
        "test_market_analyzer_generate_text.py",
        "test_market_review.py",
        "test_market_review_lock.py",
        "test_market_review_runtime.py",
        "test_name_to_code_resolver.py",
        "test_refresh_stock_index.py",
        "test_realtime_quote_fallback_logging.py",
        "test_stock_code_bse.py",
        "test_stock_code_utils.py",
        "test_stock_index_loader.py",
        "test_stock_index_remote_service.py",
        "test_stock_quote_news_api.py",
        "test_stock_watchlist_api.py",
        "test_stooq_fallback.py",
        "test_tencent_fetcher.py",
        "test_tickflow_fetcher.py",
        "test_tickflow_market_review_fallback.py",
        "test_tushare_fetcher_followups.py",
        "test_tushare_fetcher_get_stock_list.py",
        "test_tushare_fetcher_http_client.py",
        "test_us_index_mapping.py",
        "test_yfinance_fundamental_adapter.py",
        "test_yfinance_hk_indices.py",
        "test_yfinance_normalize.py",
        "test_yfinance_us_indices.py",
    }
)

# Mixed test modules still contain useful BTC/API coverage. Quarantine only the
# retired cases in those files instead of weakening the whole module.
LEGACY_STOCK_TEST_NAMES_BY_FILE = {
    "test_alert_api.py": frozenset(
        {
            "test_p6_watchlist_dry_run_aggregates_targets_without_stock_code_validation",
        }
    ),
    "test_analysis_api_contract.py": frozenset(
        {
            "test_market_review_endpoint_accepts_omitted_body",
            "test_market_review_runtime_initializes_analyzer_for_litellm_provider",
            "test_run_market_review_background_raises_when_report_is_empty",
            "test_run_market_review_background_releases_lock_on_runtime_build_failure",
            "test_run_market_review_background_returns_non_empty_result_payload",
            "test_run_market_review_background_runtime_build_failure_marks_task_failed",
            "test_run_market_review_background_uses_configured_pipeline",
            "test_trigger_market_review_accepts_background_task",
            "test_trigger_market_review_accepts_camel_case_report_language_alias",
            "test_trigger_market_review_accepts_request_level_report_language",
            "test_trigger_market_review_rejects_duplicate_submission",
            "test_trigger_market_review_rejects_when_shared_lock_is_held",
            "test_trigger_market_review_submits_even_when_configured_markets_closed",
            # Retired multi-market analysis input contract. BTC coverage lives
            # in test_btc_analysis_api_contract.py.
            "test_trigger_analysis_rejects_blank_only_stock_inputs",
            "test_trigger_analysis_rejects_obviously_invalid_mixed_input_before_resolution",
            "test_trigger_analysis_rejects_unresolvable_alpha_garbage",
            "test_trigger_analysis_accepts_us_suffix_code",
            "test_trigger_analysis_accepts_camel_case_report_language_alias",
            "test_trigger_analysis_async_passes_and_returns_analysis_phase",
            "test_trigger_analysis_accepts_hk_suffix_code_from_autocomplete",
            "test_trigger_analysis_accepts_bse_code_from_autocomplete",
            "test_trigger_analysis_accepts_bse_suffix_code_from_autocomplete",
            "test_trigger_analysis_rejects_non_bse_code_with_bj_exchange_hint",
            "test_trigger_analysis_accepts_hk_prefixed_code",
            "test_trigger_analysis_allows_stock_names_with_star_and_hyphen",
            "test_trigger_analysis_accepts_resolvable_free_text_input",
            "test_trigger_analysis_preserves_batch_metadata",
            "test_trigger_analysis_rejects_cross_request_duplicate_for_equivalent_code_shapes",
            "test_trigger_analysis_batch_does_not_apply_single_stock_name_to_all_tasks",
        }
    ),
    "test_analyzer_news_prompt.py": frozenset(
        {
            "test_analysis_prompt_keeps_injected_default_policy_for_implicit_default_run",
        }
    ),
    "test_api_schema_pydantic.py": frozenset(
        {
            "test_decision_signal_static_api_spec_matches_runtime_paths",
        }
    ),
    "test_docker_entrypoint.py": frozenset(
        {
            "test_docker_entrypoint_repairs_nested_mount_ownership",
            "test_docker_entrypoint_skips_owner_chmod_when_chown_fails",
        }
    ),
    "test_main_schedule_mode.py": frozenset(
        {
            "test_market_review_mode_uses_shared_runtime_assembly",
            "test_serve_schedule_mode_continues_scheduler_when_api_server_start_fails",
            "test_single_run_keeps_cli_stock_override",
        }
    ),
    "test_packaging_build_scripts.py": frozenset(
        {
            "test_windows_backend_build_script_does_not_collect_alphasift_adapter",
        }
    ),
    "test_pipeline_related_boards.py": frozenset(
        {
            "test_attach_belong_boards_uses_normalized_a_share_code_when_market_missing",
        }
    ),
    "test_static_assets_consistency.py": frozenset(
        {
            "test_app_startup_schedules_stock_index_background_refresh",
            "test_existing_asset_is_served_from_explicit_assets_route",
            "test_existing_asset_supports_head_and_conditional_requests",
            "test_missing_asset_returns_safe_404_content_types",
            "test_stock_index_route_does_not_parse_bundled_candidates_on_hot_path",
            "test_stock_index_route_falls_back_to_static_index",
            "test_stock_index_route_prefers_newer_static_index_over_older_remote_cache",
            "test_stock_index_route_returns_404_when_all_candidates_missing",
            "test_stock_index_route_serves_newer_remote_cache",
            "test_stock_index_route_skips_invalid_remote_cache",
        }
    ),
}


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "legacy_stock: quarantined pre-BTC-only stock-market coverage",
    )


@pytest.fixture(autouse=True)
def isolate_agent_usage_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let mocked Agent usage reach the configured runtime database."""
    runner = sys.modules.get("src.agent.runner")
    if runner is not None:
        monkeypatch.setattr(runner, "_persist_usage", lambda *_args, **_kwargs: None)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    legacy_items: list[pytest.Item] = []
    active_items: list[pytest.Item] = []
    for item in items:
        legacy_names = LEGACY_STOCK_TEST_NAMES_BY_FILE.get(item.path.name, ())
        if item.path.name in LEGACY_STOCK_TEST_FILES or item.name in legacy_names:
            item.add_marker("legacy_stock")
            legacy_items.append(item)
        else:
            active_items.append(item)

    if os.getenv("DSA_INCLUDE_LEGACY_STOCK_TESTS") == "1":
        return

    if legacy_items:
        config.hook.pytest_deselected(items=legacy_items)
        items[:] = active_items

_original_call_soon_threadsafe = asyncio.BaseEventLoop.call_soon_threadsafe


async def _shutdown_default_executor_inline(
    self: asyncio.BaseEventLoop,
    timeout: float | None = None,
) -> None:
    """Avoid lost wakeups while asyncio.run() tears down test-only executors."""
    del timeout
    executor = getattr(self, "_default_executor", None)
    if executor is None:
        return
    self._executor_shutdown_called = True
    self._default_executor = None
    executor.shutdown(wait=True)


def _call_soon_threadsafe_with_extra_wakeup(
    self: asyncio.BaseEventLoop,
    callback,
    *args,
    context=None,
):
    """Wake the selector again for sandboxed test runs where the first wake is lost."""
    handle = _original_call_soon_threadsafe(self, callback, *args, context=context)
    write_to_self = getattr(self, "_write_to_self", None)
    if callable(write_to_self):
        write_to_self()
        threading.Timer(0.001, write_to_self).start()
    return handle


asyncio.BaseEventLoop.call_soon_threadsafe = _call_soon_threadsafe_with_extra_wakeup
asyncio.BaseEventLoop.shutdown_default_executor = _shutdown_default_executor_inline


async def _run_sync_via_asyncio_to_thread(
    func: Callable[..., T],
    *args: Any,
    abandon_on_cancel: bool = False,
    cancellable: bool | None = None,
    limiter: Any = None,
) -> T:
    """Use asyncio's executor path when AnyIO worker queues miss wakeups."""
    del abandon_on_cancel, limiter
    if cancellable is not None:
        warn(
            "The `cancellable=` keyword argument to `anyio.to_thread.run_sync` is "
            "deprecated since AnyIO 4.1.0; use `abandon_on_cancel=` instead",
            DeprecationWarning,
            stacklevel=2,
        )
    future: concurrent.futures.Future[T] = concurrent.futures.Future()
    context = copy_context()

    def runner() -> None:
        try:
            future.set_result(context.run(func, *args))
        except BaseException as exc:
            future.set_exception(exc)

    threading.Thread(target=runner, name="pytest-anyio-worker", daemon=True).start()
    while not future.done():
        await asyncio.sleep(0.001)
    return future.result()


def _wait_for_cross_thread_result(loop: asyncio.AbstractEventLoop, future: concurrent.futures.Future[T]) -> T:
    write_to_self = getattr(loop, "_write_to_self", None)
    while not future.done():
        if callable(write_to_self):
            write_to_self()
        time.sleep(0.001)
    return future.result()


def _run_sync_from_thread_with_wakeup(
    cls,
    func: Callable[..., T],
    args: tuple[Any, ...],
    token: object,
) -> T:
    @wraps(func)
    def wrapper() -> None:
        try:
            _asyncio.set_current_async_library("asyncio")
            future.set_result(func(*args))
        except BaseException as exc:
            future.set_exception(exc)
            if not isinstance(exc, Exception):
                raise

    loop = token or _asyncio.threadlocals.current_token.native_token
    if loop.is_closed():
        raise _asyncio.RunFinishedError
    future: concurrent.futures.Future[T] = concurrent.futures.Future()
    loop.call_soon_threadsafe(wrapper)
    return _wait_for_cross_thread_result(loop, future)


def _run_async_from_thread_with_wakeup(
    cls,
    func: Callable[..., Awaitable[T]],
    args: tuple[Any, ...],
    token: object,
) -> T:
    loop = token or _asyncio.threadlocals.current_token.native_token
    if loop.is_closed():
        raise _asyncio.RunFinishedError
    context = copy_context()
    context.run(_asyncio.set_current_async_library, "asyncio")
    future = context.run(asyncio.run_coroutine_threadsafe, func(*args), loop=loop)
    return _wait_for_cross_thread_result(loop, future)


anyio.to_thread.run_sync = _run_sync_via_asyncio_to_thread
_asyncio.AsyncIOBackend.run_sync_from_thread = classmethod(_run_sync_from_thread_with_wakeup)
_asyncio.AsyncIOBackend.run_async_from_thread = classmethod(_run_async_from_thread_with_wakeup)


class _ThreadlessTestClient:
    """Small TestClient replacement that avoids AnyIO's cross-thread portal."""

    def __init__(
        self,
        app,
        base_url: str = "http://testserver",
        raise_server_exceptions: bool = True,
        follow_redirects: bool = True,
        **_: Any,
    ) -> None:
        self.app = app
        self.base_url = base_url
        self.raise_server_exceptions = raise_server_exceptions
        self.follow_redirects = follow_redirects
        self.cookies = httpx.Cookies()
        self._lifespan_ctx = None
        self._lifespan_enter_count = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._async_client: httpx.AsyncClient | None = None

    def _get_lifespan_context(self):
        return getattr(getattr(self.app, "router", None), "lifespan_context", None)

    def _build_async_client(self, follow_redirects: bool) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(
            app=self.app,
            raise_app_exceptions=self.raise_server_exceptions,
        )
        return httpx.AsyncClient(
            transport=transport,
            base_url=self.base_url,
            follow_redirects=follow_redirects,
            cookies=self.cookies,
        )

    def __enter__(self):
        if self._lifespan_enter_count == 0:
            self._loop = asyncio.new_event_loop()
            lifespan_context = self._get_lifespan_context()
            if callable(lifespan_context):
                self._lifespan_ctx = lifespan_context(self.app)
                self._loop.run_until_complete(self._lifespan_ctx.__aenter__())
            self._async_client = self._build_async_client(self.follow_redirects)
        self._lifespan_enter_count += 1
        return self

    def __exit__(self, *args: Any) -> None:
        if self._lifespan_enter_count == 0:
            return None

        self._lifespan_enter_count -= 1
        if self._lifespan_enter_count == 0 and self._loop is not None:
            try:
                async def _close() -> None:
                    if self._async_client is not None:
                        await self._async_client.aclose()
                    if self._lifespan_ctx is not None:
                        await self._lifespan_ctx.__aexit__(*args)

                self._loop.run_until_complete(_close())
            finally:
                self._lifespan_ctx = None
                self._async_client = None
                self._loop.close()
                self._loop = None
        return None

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        follow_redirects = kwargs.pop("follow_redirects", self.follow_redirects)
        kwargs.pop("allow_redirects", None)

        if self._lifespan_enter_count > 0 and self._loop is not None and self._async_client is not None:
            response = self._loop.run_until_complete(
                self._async_client.request(method, url, follow_redirects=follow_redirects, **kwargs)
            )
            self.cookies = httpx.Cookies(self._async_client.cookies)
            return response

        async def _send() -> httpx.Response:
            async with self._build_async_client(follow_redirects) as client:
                response = await client.request(method, url, **kwargs)
                self.cookies = httpx.Cookies(client.cookies)
                return response

        return asyncio.run(_send())

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("PATCH", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("HEAD", url, **kwargs)


fastapi.testclient.TestClient = _ThreadlessTestClient
starlette.testclient.TestClient = _ThreadlessTestClient
