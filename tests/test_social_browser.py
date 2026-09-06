import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.config import Settings
from src.parsers import social_browser
from src.parsers.base import LinkMetadata, ParserError

_URL = "https://www.tiktok.com/@writer/video/1234567890"
_IMAGE = "https://cdn.example/opaque?signature=keep"


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(
        cookie_file=str(tmp_path / "missing-cookies"),
        card_browser_profile_dir=str(tmp_path / "cards"),
    ).model_copy(update=overrides)


def _html() -> str:
    payload = {"id": "1234567890", "desc": "Verified caption", "video": {"cover": _IMAGE}}
    return f'<script type="application/json">{json.dumps(payload)}</script>'


class FakePage:
    def __init__(self, url: str = _URL, html: str = "") -> None:
        self.url = url
        self.main_frame = SimpleNamespace(url=url)
        self.html = html
        self.listeners: dict[str, Any] = {}
        self.route = AsyncMock()
        self.goto = AsyncMock(return_value=None)
        self.content = AsyncMock(side_effect=lambda: self.html)

    def on(self, name: str, callback: Any) -> None:
        self.listeners[name] = callback

    def remove_listener(self, name: str, callback: Any) -> None:
        assert self.listeners[name] == callback
        del self.listeners[name]


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.pages = [page]
        self.add_cookies = AsyncMock()


def _install_context(
    monkeypatch: pytest.MonkeyPatch, page: FakePage, *, close_delay: float = 0
) -> tuple[dict[str, Any], FakeContext, asyncio.Event]:
    seen: dict[str, Any] = {}
    context = FakeContext(page)
    closed = asyncio.Event()

    @asynccontextmanager
    async def open_context(profile: str, **kwargs: Any) -> AsyncIterator[FakeContext]:
        seen.update(profile=profile, **kwargs)
        try:
            yield context
        finally:
            await asyncio.sleep(close_delay)
            closed.set()

    monkeypatch.setattr(social_browser, "persistent_context", open_context)
    monkeypatch.setattr(social_browser, "chrome_user_agent", lambda: "Installed Chrome UA")
    monkeypatch.setattr(social_browser, "playwright_cookies_from_file", lambda *_: [])
    return seen, context, closed


async def test_browser_uses_private_profile_and_reads_only_card_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = FakePage(html=_html())
    seen, context, closed = _install_context(monkeypatch, page)
    cookies = [{"name": "sessionid", "value": "test-fixture", "domain": ".tiktok.com", "path": "/"}]
    monkeypatch.setattr(social_browser, "playwright_cookies_from_file", lambda *_: cookies)

    result = await social_browser.SocialBrowserParser(_settings(tmp_path)).parse(_URL)

    assert result.title == "Verified caption"
    assert result.cover_url == _IMAGE
    assert seen["profile"] == str(tmp_path / "cards" / "tiktok")
    assert seen["channel"] == "chrome"
    assert seen["headless"] is False
    assert "--headless=new" in seen["extra_args"]
    assert "--disable-gpu" in seen["omit_args"]
    assert seen["close_timeout_seconds"] <= 3
    assert seen["user_agent"] == "Installed Chrome UA"
    context.add_cookies.assert_awaited_once_with(cookies)
    page.goto.assert_awaited_once()
    assert page.goto.await_args.args == (_URL,)
    assert not page.listeners
    assert closed.is_set()
    assert result.download_candidates == []


async def test_browser_captures_target_json_from_page_generated_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = FakePage()
    _install_context(monkeypatch, page)

    class Response:
        url = "https://www.tiktok.com/api/item/detail/?itemId=1234567890"

        def __init__(self) -> None:
            self.headers = {"content-type": "application/json"}

        async def text(self) -> str:
            return json.dumps(
                {
                    "itemInfo": {
                        "itemStruct": {
                            "id": "1234567890",
                            "desc": "Network caption",
                            "video": {"cover": _IMAGE},
                        }
                    }
                }
            )

    async def navigate(*args: Any, **kwargs: Any) -> None:
        page.listeners["response"](Response())
        await asyncio.sleep(0)

    page.goto.side_effect = navigate
    result = await social_browser.SocialBrowserParser(_settings(tmp_path)).parse(_URL)

    assert result.description == "Network caption"
    assert not page.listeners


@pytest.mark.parametrize(
    "url,platform,allowed",
    [
        ("https://www.tiktok.com/api/item/detail/?itemId=123", "tiktok", True),
        ("https://www.tiktok.com/api/comment/list/?aweme_id=123", "tiktok", False),
        ("https://www.tiktok.com/api/related/item_list/", "tiktok", False),
        ("https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=123", "douyin", True),
        ("https://x.com/i/api/graphql/abc/TweetDetail", "x", True),
        ("https://x.com/i/api/graphql/abc/HomeTimeline", "x", False),
        ("https://attacker.test/api/item/detail/", "tiktok", False),
    ],
)
def test_response_listener_ignores_comments_recommendations_and_other_domains(
    url: str, platform: str, allowed: bool
) -> None:
    assert social_browser._response_allowed(url, platform) is allowed


async def test_browser_timeout_leaves_time_for_context_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = FakePage()
    _, _, closed = _install_context(monkeypatch, page, close_delay=0.01)
    navigate_cancelled = asyncio.Event()

    async def hang(*args: Any, **kwargs: Any) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            navigate_cancelled.set()

    page.goto.side_effect = hang
    parser = social_browser.SocialBrowserParser(_settings(tmp_path, card_browser_timeout=0.12))
    started = time.monotonic()
    with pytest.raises(ParserError, match="timeout"):
        await parser.parse(_URL)

    assert time.monotonic() - started < 0.2
    assert navigate_cancelled.is_set()
    assert closed.is_set()
    assert not page.listeners


async def test_external_cancellation_propagates_and_closes_private_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = FakePage()
    _, _, closed = _install_context(monkeypatch, page)
    started = asyncio.Event()

    async def hang(*args: Any, **kwargs: Any) -> None:
        started.set()
        await asyncio.Event().wait()

    page.goto.side_effect = hang
    parser = social_browser.SocialBrowserParser(_settings(tmp_path))
    task = asyncio.create_task(parser.parse(_URL))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    assert not page.listeners


async def test_concurrency_is_shared_across_instances_and_serial_within_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active: dict[str, int] = {}
    max_total = 0
    max_platform = 0

    async def collect(self: Any, url: str, platform: str, deadline: float) -> LinkMetadata:
        nonlocal max_total, max_platform
        active[platform] = active.get(platform, 0) + 1
        max_total = max(max_total, sum(active.values()))
        max_platform = max(max_platform, active[platform])
        await asyncio.sleep(0.015)
        active[platform] -= 1
        return LinkMetadata(source_url=url, title="Target")

    monkeypatch.setattr(social_browser.SocialBrowserParser, "_collect", collect)
    urls = [_URL, _URL, "https://www.douyin.com/video/123", "https://x.com/w/status/123"]
    await asyncio.gather(
        *(social_browser.SocialBrowserParser(_settings(tmp_path)).parse(url) for url in urls)
    )
    assert max_total == 2
    assert max_platform == 1


async def test_queue_wait_is_part_of_the_browser_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, card_browser_timeout=0.03)
    _, platform_sem = social_browser._limits("tiktok", settings)
    collect = AsyncMock(side_effect=AssertionError("must not start without a slot"))
    monkeypatch.setattr(social_browser.SocialBrowserParser, "_collect", collect)
    async with platform_sem:
        with pytest.raises(ParserError, match="timeout"):
            await social_browser.SocialBrowserParser(settings).parse(_URL)
    collect.assert_not_awaited()


async def test_page_snapshot_racing_a_client_redirect_retries_within_same_visit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = FakePage()
    _, _, closed = _install_context(monkeypatch, page)
    page.content.side_effect = [
        RuntimeError("Page.content: Unable to retrieve content because the page is navigating"),
        _html(),
    ]
    monkeypatch.setattr(social_browser, "_POLL_SECONDS", 0)

    result = await social_browser.SocialBrowserParser(_settings(tmp_path)).parse(_URL)

    assert result.title == "Verified caption"
    assert page.content.await_count == 2
    page.goto.assert_awaited_once()
    assert closed.is_set()


@pytest.mark.parametrize("via", ["redirect", "frame", "chain"])
async def test_short_link_locks_original_note_before_recommendation_redirect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, via: str
) -> None:
    short = "https://v.douyin.com/example/"
    target = "https://www.douyin.com/note/7418562738437229874"
    recommended = "https://www.douyin.com/jingxuan?modal_id=7681978918144404154"
    payload = {
        "aweme_id": "7681978918144404154",
        "desc": "Unrelated recommendation",
        "video": {"cover": {"url_list": [_IMAGE]}},
    }
    page = FakePage(short, f'<script type="application/json">{json.dumps(payload)}</script>')
    _, _, closed = _install_context(monkeypatch, page)

    def request(request_url: str, previous: Any = None) -> Any:
        return SimpleNamespace(
            url=request_url,
            frame=page.main_frame,
            redirected_from=previous,
            is_navigation_request=lambda: True,
        )

    async def navigate(*args: Any, **kwargs: Any) -> Any:
        if via == "redirect":
            page.listeners["response"](
                SimpleNamespace(
                    url=short, status=302, headers={"location": target}, request=request(short)
                )
            )
        elif via == "frame":
            page.main_frame.url = target
            page.listeners["framenavigated"](page.main_frame)
        page.url = recommended
        page.main_frame.url = recommended
        if via == "chain":
            return SimpleNamespace(
                url=recommended,
                status=200,
                headers={},
                request=request(recommended, request(target, request(short))),
            )
        page.listeners["framenavigated"](page.main_frame)
        return None

    page.goto.side_effect = navigate
    with pytest.raises(ParserError, match="target_mismatch") as caught:
        await social_browser.SocialBrowserParser(_settings(tmp_path)).parse(short)

    assert caught.value.url == short
    assert closed.is_set()
    assert not page.listeners


async def test_successful_short_link_keeps_original_source_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    short = "https://v.douyin.com/example/"
    target = "https://www.douyin.com/note/7418562738437229874"
    payload = {
        "aweme_id": "7418562738437229874",
        "desc": "Target photo caption",
        "images": [{"url_list": [_IMAGE]}],
    }
    page = FakePage(target, f'<script type="application/json">{json.dumps(payload)}</script>')
    _install_context(monkeypatch, page)

    result = await social_browser.SocialBrowserParser(_settings(tmp_path)).parse(short)

    assert result.source_url == short
    assert result.canonical_url == target
    assert result.description == "Target photo caption"
    assert result.cover_url == _IMAGE
