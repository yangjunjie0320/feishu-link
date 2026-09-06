from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from src.bibi_client import BibiAPIError, BibiClient
from src.config import Settings
from src.cookie_refresh import write_netscape
from src.cookie_utils import get_cookie_header


def _merge_cookies(
    current: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    # Playwright merges cookie identities; it never drops omitted cookies.
    combined = {
        (cookie["name"], cookie["domain"], cookie["path"], cookie.get("partitionKey")): dict(cookie)
        for cookie in [*current, *incoming]
    }
    for cookie in combined.values():
        cookie.setdefault("sameSite", "Lax")
    return list(combined.values())


class _RequestPage:
    def __init__(self, context: _RequestContext) -> None:
        self.context = context
        self.visited: list[str] = []
        self.navigation_error: Exception | None = None

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.visited.append(url)
        # Model the app refreshing Supabase cookies before a request starts.
        self.context.cookie_jar[0]["value"] = "refreshed-token"
        if self.navigation_error is not None:
            raise self.navigation_error


class _RequestContext:
    def __init__(self) -> None:
        self.cookie_jar: list[dict[str, Any]] = []
        self.local_storage: dict[str, str] = {}
        self.page = _RequestPage(self)
        self.closed = False
        self.close_error: Exception | None = None
        self.copy_error: Exception | None = None

    async def add_cookies(self, cookies: list[dict[str, Any]]) -> None:
        if self.copy_error is not None:
            self.cookie_jar = [dict(cookie) for cookie in cookies[:1]]
            raise self.copy_error
        self.cookie_jar = _merge_cookies(self.cookie_jar, cookies)

    async def clear_cookies(self, **filters: str) -> None:
        self.cookie_jar = [
            cookie
            for cookie in self.cookie_jar
            if not all(cookie[key] == value for key, value in filters.items())
        ]

    async def cookies(self) -> list[dict[str, Any]]:
        return [dict(cookie) for cookie in self.cookie_jar]

    async def new_page(self) -> _RequestPage:
        return self.page

    async def close(self) -> None:
        await asyncio.sleep(0)
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class _Browser:
    def __init__(self, request_context: _RequestContext) -> None:
        self.request_context = request_context
        self.options: dict[str, Any] = {}

    async def new_context(self, **kwargs: Any) -> _RequestContext:
        self.options = kwargs
        assert "storage_state" not in kwargs, "request contexts must not inherit saved queues"
        return self.request_context


class _Profile:
    def __init__(self, browser: _Browser) -> None:
        self.browser = browser
        self.local_storage = {"task-queue-storage": "old unfinished task"}
        self.closed = False
        self.sync_error: Exception | None = None
        self.cookie_jar: list[dict[str, Any]] = [
            {
                "name": "sb-test-auth-token",
                "value": "original-token",
                "domain": "aitodo.co",
                "path": "/",
                "expires": 2147483647,
                "secure": True,
                "httpOnly": False,
                "sameSite": "Lax",
            }
        ]

    @property
    def pages(self) -> list[Any]:
        raise AssertionError("the persistent profile's old pages must not be used")

    async def storage_state(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("origins/localStorage must not be copied")

    async def cookies(self) -> list[dict[str, Any]]:
        return [dict(cookie) for cookie in self.cookie_jar]

    async def add_cookies(self, cookies: list[dict[str, Any]]) -> None:
        if self.sync_error is not None:
            raise self.sync_error
        self.cookie_jar = _merge_cookies(self.cookie_jar, cookies)

    async def clear_cookies(self, **filters: str) -> None:
        self.cookie_jar = [
            cookie
            for cookie in self.cookie_jar
            if not all(cookie[key] == value for key, value in filters.items())
        ]


def _setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, writeback: bool = True
) -> tuple[BibiClient, _Profile, _RequestContext, Path]:
    request_context = _RequestContext()
    profile = _Profile(_Browser(request_context))

    @asynccontextmanager
    async def persistent_context(*args: Any, **kwargs: Any) -> AsyncIterator[_Profile]:
        try:
            yield profile
        finally:
            await asyncio.sleep(0)
            profile.closed = True

    monkeypatch.setattr("src.bibi_client.persistent_context", persistent_context)
    cookie_path = tmp_path / "bibigpt.txt"
    client = BibiClient(
        Settings(
            cookie_file="",
            platform_cookie_files={"bibigpt": str(cookie_path)},
            bibigpt_access_mode="browser",
            bibigpt_cookie_writeback=writeback,
        )
    )
    return client, profile, request_context, cookie_path


def _assert_refreshed_and_closed(
    profile: _Profile, request: _RequestContext, cookie_path: Path
) -> None:
    assert profile.cookie_jar[0]["value"] == "refreshed-token"
    assert "sb-test-auth-token=refreshed-token" in get_cookie_header(str(cookie_path), "aitodo.co")
    assert profile.closed
    assert request.closed
    assert profile.local_storage == {"task-queue-storage": "old unfinished task"}
    assert request.local_storage == {}


async def test_browser_page_isolates_old_queue_and_saves_refreshed_cookies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, profile, request, cookie_path = _setup(monkeypatch, tmp_path)

    async with client._browser_page() as page:
        assert page is request.page
        assert request.cookie_jar[0]["value"] == "refreshed-token"
        assert request.page.visited == ["https://aitodo.co/zh/desktop"]

    _assert_refreshed_and_closed(profile, request, cookie_path)


async def test_failed_request_still_saves_refreshed_cookies_and_closes_contexts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, profile, request, cookie_path = _setup(monkeypatch, tmp_path)
    failure = BibiAPIError(500, "upstream network error")

    with pytest.raises(BibiAPIError) as raised:
        async with client._browser_page():
            raise failure

    assert raised.value is failure
    _assert_refreshed_and_closed(profile, request, cookie_path)


async def test_cancelled_request_saves_refreshed_cookies_before_propagating(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, profile, request, cookie_path = _setup(monkeypatch, tmp_path)
    entered = asyncio.Event()

    async def run() -> None:
        async with client._browser_page():
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(run())
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    _assert_refreshed_and_closed(profile, request, cookie_path)


async def test_navigation_failure_saves_refreshed_cookies_and_closes_contexts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, profile, request, cookie_path = _setup(monkeypatch, tmp_path)
    request.page.navigation_error = RuntimeError("navigation failed after cookie refresh")

    with pytest.raises(RuntimeError, match="navigation failed"):
        async with client._browser_page():
            pytest.fail("a failed navigation must not yield a page")

    _assert_refreshed_and_closed(profile, request, cookie_path)


async def test_disabled_writeback_still_syncs_live_profile_without_creating_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, profile, request, cookie_path = _setup(monkeypatch, tmp_path, writeback=False)
    async with client._browser_page():
        pass

    assert profile.cookie_jar[0]["value"] == "refreshed-token"
    assert not cookie_path.exists()
    assert profile.closed
    assert request.closed


async def test_cleanup_failures_do_not_hide_request_error_or_skip_other_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client, profile, request, cookie_path = _setup(monkeypatch, tmp_path)
    profile.sync_error = RuntimeError("profile write failed")
    request.close_error = RuntimeError("context close failed")
    failure = BibiAPIError(500, "original request error")

    with pytest.raises(BibiAPIError) as raised:
        async with client._browser_page():
            raise failure

    assert raised.value is failure
    assert "sb-test-auth-token=refreshed-token" in get_cookie_header(str(cookie_path), "aitodo.co")
    assert "Failed to sync BibiGPT browser cookies" in caplog.text
    assert "Failed to close BibiGPT request context" in caplog.text
    assert profile.closed
    assert request.closed


async def test_refreshed_cookie_snapshot_removes_old_auth_tail_without_losing_other_cookies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, profile, request, cookie_path = _setup(monkeypatch, tmp_path)
    template = profile.cookie_jar[0]
    profile.cookie_jar = [
        {**template, "name": f"sb-test-auth-token.{index}", "value": f"old-{index}"}
        for index in range(3)
    ] + [{**template, "name": "theme", "value": "dark"}]

    async with client._browser_page():
        await request.clear_cookies(name="sb-test-auth-token.2", domain="aitodo.co", path="/")
        await request.add_cookies(
            [
                {**template, "name": f"sb-test-auth-token.{index}", "value": f"new-{index}"}
                for index in range(2)
            ]
        )

    values = {cookie["name"]: cookie["value"] for cookie in profile.cookie_jar}
    assert values == {
        "sb-test-auth-token.0": "new-0",
        "sb-test-auth-token.1": "new-1",
        "theme": "dark",
    }
    assert "sb-test-auth-token.2=" not in get_cookie_header(str(cookie_path), "aitodo.co")
    assert profile.local_storage == {"task-queue-storage": "old unfinished task"}


async def test_partial_initial_cookie_copy_never_syncs_back_or_overwrites_cookie_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, profile, request, cookie_path = _setup(monkeypatch, tmp_path)
    profile.cookie_jar.append({**profile.cookie_jar[0], "name": "theme", "value": "dark"})
    write_netscape(profile.cookie_jar, str(cookie_path))
    original_file = cookie_path.read_text()
    original_cookies = await profile.cookies()
    request.copy_error = RuntimeError("initial copy failed halfway")

    with pytest.raises(RuntimeError, match="initial copy failed halfway"):
        async with client._browser_page():
            pytest.fail("incomplete cookie initialization cannot yield a page")

    assert profile.cookie_jar == original_cookies
    assert cookie_path.read_text() == original_file
    assert profile.closed
    assert request.closed


@pytest.mark.parametrize(
    "new_names", [["sb-test-auth-token"], ["sb-test-auth-token.0", "sb-test-auth-token.1"]]
)
async def test_cookie_file_seed_replaces_only_its_auth_family(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, new_names: list[str]
) -> None:
    client, profile, _request, cookie_path = _setup(monkeypatch, tmp_path)
    template = profile.cookie_jar[0]
    preserved = [
        {**template, "name": "theme", "value": "dark"},
        {**template, "name": "sb-other-auth-token.0", "value": "another-project"},
        {**template, "name": "sb-test-auth-token.2", "domain": "other.test", "value": "other-site"},
    ]
    profile.cookie_jar = [
        {**template, "name": f"sb-test-auth-token.{index}", "value": f"old-{index}"}
        for index in range(3)
    ] + preserved
    fresh = [
        {**template, "name": name, "value": f"fresh-{index}"}
        for index, name in enumerate(new_names)
    ]
    write_netscape(fresh, str(cookie_path))

    await client._seed_browser_cookies(profile)

    actual = {(cookie["name"], cookie["domain"]): cookie["value"] for cookie in profile.cookie_jar}
    expected = {
        (cookie["name"], cookie["domain"]): cookie["value"] for cookie in [*fresh, *preserved]
    }
    assert actual == expected
    assert profile.local_storage == {"task-queue-storage": "old unfinished task"}
