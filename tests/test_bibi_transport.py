from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.bibi_client import (
    AuthenticationError,
    BibiAPIError,
    BibiClient,
    BibiTimeoutError,
    _extract_trpc_data,
)
from src.config import Settings


def _client(timeout: float = 0.02) -> BibiClient:
    return BibiClient(
        Settings(
            cookie_file="",
            bibigpt_access_mode="browser",
            bibigpt_browser_timeout=timeout,
            bibigpt_cookie_writeback=False,
        )
    )


class _Page:
    def __init__(self, *, hang_navigation: bool = False, hang_fetch: bool = False) -> None:
        self.hang_navigation = hang_navigation
        self.hang_fetch = hang_fetch
        self.fetch_started = asyncio.Event()
        self.fetch_cancelled = asyncio.Event()

    async def goto(self, *args: Any, **kwargs: Any) -> None:
        if self.hang_navigation:
            await asyncio.Event().wait()

    async def evaluate(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.fetch_started.set()
        if self.hang_fetch:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.fetch_cancelled.set()
                raise
        return {"status": 200, "text": '{"ready": true}'}


def _install_context(
    monkeypatch: pytest.MonkeyPatch, page: _Page, lock: asyncio.Lock
) -> asyncio.Event:
    closed = asyncio.Event()

    class RequestContext:
        async def add_cookies(self, cookies: list[dict[str, Any]]) -> None:
            pass

        async def cookies(self) -> list[dict[str, Any]]:
            return []

        async def new_page(self) -> _Page:
            return page

        async def close(self) -> None:
            await asyncio.sleep(0)

    class Browser:
        async def new_context(self, **kwargs: Any) -> RequestContext:
            return RequestContext()

    class Context(RequestContext):
        def __init__(self) -> None:
            self.browser = Browser()

    @asynccontextmanager
    async def persistent_context(*args: Any, **kwargs: Any) -> AsyncIterator[Context]:
        async with lock:
            try:
                yield Context()
            finally:
                # Exercise awaited cleanup after timeout/cancellation as the
                # real persistent context does when closing Chromium.
                await asyncio.sleep(0)
                closed.set()

    monkeypatch.setattr("src.bibi_client.persistent_context", persistent_context)
    return closed


async def test_browser_fetch_bounds_hung_evaluate_and_releases_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _Page(hang_fetch=True)
    lock = asyncio.Lock()
    closed = _install_context(monkeypatch, page, lock)

    with pytest.raises(BibiTimeoutError, match=r"timed out after 0\.02 seconds"):
        await asyncio.wait_for(_client()._browser_fetch_json("https://aitodo.co/api", {}), 1)

    assert page.fetch_cancelled.is_set()
    assert closed.is_set()
    assert not lock.locked()
    page.hang_fetch = False
    assert await _client()._browser_fetch_json("https://aitodo.co/api", {}) == {"ready": True}


async def test_browser_fetch_bounds_profile_lock_wait_without_closing_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _Page()
    lock = asyncio.Lock()
    closed = _install_context(monkeypatch, page, lock)
    await lock.acquire()
    try:
        with pytest.raises(BibiTimeoutError):
            await asyncio.wait_for(_client()._browser_fetch_json("https://aitodo.co/api", {}), 1)
        assert lock.locked()
        assert not closed.is_set()
        assert not page.fetch_started.is_set()
    finally:
        lock.release()

    assert await _client()._browser_fetch_json("https://aitodo.co/api", {}) == {"ready": True}
    assert closed.is_set()
    assert not lock.locked()


async def test_browser_fetch_bounds_navigation_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _Page(hang_navigation=True)
    lock = asyncio.Lock()
    closed = _install_context(monkeypatch, page, lock)

    with pytest.raises(BibiTimeoutError):
        await asyncio.wait_for(_client()._browser_fetch_json("https://aitodo.co/api", {}), 1)

    assert closed.is_set()
    assert not lock.locked()
    assert not page.fetch_started.is_set()


async def test_external_cancellation_propagates_after_browser_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _Page(hang_fetch=True)
    lock = asyncio.Lock()
    closed = _install_context(monkeypatch, page, lock)
    task = asyncio.create_task(_client(10)._browser_fetch_json("https://aitodo.co/api", {}))
    await asyncio.wait_for(page.fetch_started.wait(), 1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert closed.is_set()
    assert page.fetch_cancelled.is_set()
    assert not lock.locked()


async def test_request_on_existing_page_also_bounds_hung_evaluate() -> None:
    page = _Page(hang_fetch=True)
    with pytest.raises(BibiTimeoutError):
        await asyncio.wait_for(
            _client()._browser_request_json(page, "https://aitodo.co/api", {}), 1
        )
    assert page.fetch_cancelled.is_set()


async def test_browser_abort_is_reported_as_timeout() -> None:
    page = _Page()
    page.evaluate = AsyncMock(return_value={"timedOut": True})  # type: ignore[method-assign]

    with pytest.raises(BibiTimeoutError):
        await _client()._browser_request_json(page, "https://aitodo.co/api", {})


@pytest.mark.parametrize("status", [401, 403, 402, 429, 500, 502, 504])
@pytest.mark.parametrize("batched", [False, True])
def test_embedded_trpc_error_uses_business_http_status(status: int, batched: bool) -> None:
    error = {
        "json": {
            "message": "平台风控，稍后再试" if status == 500 else "server reason",
            "code": -32603,
            "data": {"httpStatus": status, "code": "INTERNAL_SERVER_ERROR"},
        }
    }
    payload: Any = {"error": error}
    if batched:
        payload = [payload]
    expected_type = AuthenticationError if status in (401, 403) else BibiAPIError

    with pytest.raises(expected_type) as raised:
        _extract_trpc_data(payload)

    assert raised.value.status_code == status
    assert json.loads(raised.value.body) == error


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("UNAUTHORIZED", 401),
        ("FORBIDDEN", 403),
        ("PAYMENT_REQUIRED", 402),
        ("TOO_MANY_REQUESTS", 429),
        ("INTERNAL_SERVER_ERROR", 500),
        ("BAD_GATEWAY", 502),
        ("GATEWAY_TIMEOUT", 504),
    ],
)
def test_embedded_trpc_error_falls_back_to_business_code(code: str, status: int) -> None:
    with pytest.raises(BibiAPIError) as raised:
        _extract_trpc_data(
            {"error": {"json": {"message": "upstream failed", "data": {"code": code}}}}
        )

    assert raised.value.status_code == status
    assert isinstance(raised.value, AuthenticationError) == (status in (401, 403))


def test_unknown_embedded_error_keeps_transport_status_and_body() -> None:
    error = {"json": {"message": "unexpected", "data": {"code": "UNKNOWN"}}}
    with pytest.raises(BibiAPIError) as raised:
        _extract_trpc_data({"error": error})
    assert raised.value.status_code == 200
    assert json.loads(raised.value.body) == error


@pytest.mark.parametrize(
    ("code", "status"),
    [(-32603, 500), (-32001, 401), (-32002, 402), (-32003, 403), (-32029, 429)],
)
def test_embedded_trpc_error_without_metadata_uses_numeric_code(code: int, status: int) -> None:
    with pytest.raises(BibiAPIError) as raised:
        _extract_trpc_data({"error": {"json": {"message": "平台风控", "code": code}}})
    assert raised.value.status_code == status
    assert isinstance(raised.value, AuthenticationError) == (status in (401, 403))


def test_successful_trpc_response_is_unchanged() -> None:
    summary = {"summary": "已完成", "contentId": "content-1"}
    assert _extract_trpc_data([{"result": {"data": {"json": summary}}}]) == summary
