import asyncio
import json
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import httpx
import pytest

from src.card import build_card
from src.card_metadata import CardSourceError, missing_card_metrics
from src.config import Settings
from src.dispatch import Dispatcher
from src.parsers.base import CardStatus, LinkMetadata, MediaType, ParserError

YT = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
TT = "https://www.tiktok.com/@creator/video/123456789"


def metadata(url: str, *, counts: bool = False) -> LinkMetadata:
    return LinkMetadata(
        source_url=url, platform="youtube" if "youtube" in url else "tiktok",
        title="Original video title", description="Original caption",
        cover_url="https://cdn.example/cover.jpg", media_type=MediaType.VIDEO,
        content_verified=True, view_count=12000 if counts else None,
        like_count=0 if counts else None, comment_count=42 if counts else None,
        repost_count=7 if counts and "tiktok" in url else None,
    )


@pytest.fixture
async def dispatcher() -> AsyncIterator[Dispatcher]:
    async with httpx.AsyncClient() as client:
        instance = Dispatcher(Settings(cookie_refresh_enabled=False), client)
        instance._oembed = AsyncMock()
        instance._og = AsyncMock()
        instance._browser = AsyncMock()
        instance._ytdlp = AsyncMock()
        instance._og.parse.side_effect = ParserError(YT, "page unavailable")
        instance._browser.parse_metrics.side_effect = ParserError(YT, "browser unavailable")
        yield instance


@pytest.mark.parametrize("url", [YT, TT])
async def test_complete_oembed_card_gets_counters_before_rendering(
    dispatcher: Dispatcher, url: str,
) -> None:
    dispatcher._oembed.parse.return_value = metadata(url)
    page = metadata(url, counts=True)
    page.title = "Other page title"
    page.description = "Other caption"
    dispatcher._og.parse.side_effect = None
    dispatcher._og.parse.return_value = page
    result = await dispatcher.parse_card(url)
    assert result.status == CardStatus.COMPLETE
    assert result.metadata.title == "Original video title"
    assert result.metadata.description == "Original caption"
    assert not missing_card_metrics(result.metadata)
    assert result.sources[-1] == "page_metrics"
    card = json.dumps(json.loads(build_card(result.metadata)), ensure_ascii=False)
    assert "播放 1.2万" in card
    assert "点赞 0" in card
    assert "评论 42" in card
    if url == TT:
        assert "转发 7" in card
    dispatcher._browser.parse_metrics.assert_not_called()
    dispatcher._ytdlp.parse.assert_not_called()


async def test_configured_youtube_api_with_counters_does_not_need_more_requests(
    dispatcher: Dispatcher,
) -> None:
    dispatcher._settings.youtube_api_key = "test"
    dispatcher._youtube = AsyncMock()
    dispatcher._youtube.parse_api.return_value = metadata(YT, counts=True)
    result = await dispatcher.parse_card(YT)
    assert not missing_card_metrics(result.metadata)
    dispatcher._oembed.parse.assert_not_called()
    dispatcher._og.parse.assert_not_called()
    dispatcher._browser.parse_metrics.assert_not_called()


async def test_page_partial_metrics_use_browser_and_never_overwrite_zero(
    dispatcher: Dispatcher,
) -> None:
    dispatcher._oembed.parse.return_value = metadata(TT)
    page = metadata(TT)
    page.view_count = 0
    dispatcher._og.parse.side_effect = None
    dispatcher._og.parse.return_value = page
    dispatcher._browser.parse_metrics.side_effect = None
    dispatcher._browser.parse_metrics.return_value = metadata(TT, counts=True)
    result = await dispatcher.parse_card(TT)
    assert result.metadata.view_count == 0
    assert result.metadata.like_count == 0
    assert not missing_card_metrics(result.metadata)
    assert result.sources == ["tiktok_oembed", "page_metrics", "browser_metrics"]
    dispatcher._og.parse.assert_awaited_once()
    dispatcher._browser.parse.assert_not_called()


@pytest.mark.parametrize("via_browser", [False, True])
async def test_existing_content_sources_are_not_fetched_again(
    dispatcher: Dispatcher, via_browser: bool,
) -> None:
    dispatcher._oembed.parse.side_effect = ParserError(YT, "failed")
    if via_browser:
        dispatcher._browser.parse_metrics.side_effect = None
        dispatcher._browser.parse_metrics.return_value = metadata(YT)
    else:
        dispatcher._og.parse.side_effect = None
        dispatcher._og.parse.return_value = metadata(YT)
    result = await dispatcher.parse_card(YT)
    assert result.status == CardStatus.COMPLETE
    dispatcher._og.parse.assert_awaited_once()
    dispatcher._browser.parse_metrics.assert_awaited_once()


async def test_metrics_share_one_timeout_keep_content_and_cancel_browser(
    dispatcher: Dispatcher,
) -> None:
    dispatcher._settings.card_metrics_timeout = 0.04
    dispatcher._oembed.parse.return_value = metadata(TT)
    cancelled = asyncio.Event()

    async def hang(*args: object, **kwargs: object) -> LinkMetadata:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    dispatcher._og.parse.side_effect = hang
    dispatcher._browser.parse_metrics.side_effect = hang
    async with asyncio.timeout(0.3):
        result = await dispatcher.parse_card(TT)
    assert result.status == CardStatus.COMPLETE
    assert result.reason == ""
    assert result.metadata.title == "Original video title"
    assert cancelled.is_set()
    dispatcher._browser.parse_metrics.assert_awaited_once()


async def test_metrics_remain_inside_total_parse_budget(dispatcher: Dispatcher) -> None:
    dispatcher._settings.card_parse_timeout = 0.04
    dispatcher._oembed.parse.return_value = metadata(YT)

    async def hang(*args: object, **kwargs: object) -> LinkMetadata:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    dispatcher._og.parse.side_effect = hang
    dispatcher._browser.parse_metrics.side_effect = hang
    async with asyncio.timeout(0.3):
        result = await dispatcher.parse_card(YT)
    assert result.status == CardStatus.COMPLETE
    assert result.metadata.cover_url


@pytest.mark.parametrize("kind", ["auth", "challenge", "deleted", "geo", "rate_limit"])
async def test_access_restrictions_do_not_launch_extra_browser(
    dispatcher: Dispatcher, kind: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatcher._oembed.parse.return_value = metadata(TT)
    dispatcher._og.parse.side_effect = CardSourceError(TT, kind, kind=kind, retry_after=90)
    refresh = AsyncMock(return_value=False)
    monkeypatch.setattr("src.dispatch.force_refresh", refresh)
    result = await dispatcher.parse_card(TT)
    assert result.status == CardStatus.COMPLETE
    dispatcher._browser.parse_metrics.assert_not_called()
    assert refresh.await_count == (1 if kind == "auth" else 0)
    if kind == "rate_limit":
        assert dispatcher._rate_limited_until["tiktok"] > asyncio.get_running_loop().time()


async def test_wrong_target_metrics_are_not_merged(dispatcher: Dispatcher) -> None:
    dispatcher._oembed.parse.return_value = metadata(YT)
    dispatcher._og.parse.side_effect = None
    dispatcher._og.parse.return_value = metadata(
        "https://www.youtube.com/watch?v=abcdefghijk", counts=True,
    )
    result = await dispatcher.parse_card(YT)
    assert result.status == CardStatus.COMPLETE
    assert result.metadata.view_count is None
    dispatcher._browser.parse_metrics.assert_not_called()


async def test_duplicate_shares_share_metrics_and_cache_returns_independent_copies(
    dispatcher: Dispatcher,
) -> None:
    dispatcher._oembed.parse.return_value = metadata(YT)
    dispatcher._og.parse.side_effect = None
    dispatcher._og.parse.return_value = metadata(YT, counts=True)
    short = "https://youtu.be/dQw4w9WgXcQ"
    first, second = await asyncio.gather(dispatcher.parse_card(YT), dispatcher.parse_card(short))
    assert first.metadata.source_url == YT
    assert second.metadata.source_url == short
    first.metadata.like_count = 100
    assert second.metadata.like_count == 0
    cached = await dispatcher.parse_card(YT)
    assert cached.metadata.like_count == 0
    dispatcher._og.parse.assert_awaited_once()


async def test_tiktok_photo_and_resolved_short_share_get_metrics(dispatcher: Dispatcher) -> None:
    short = "https://vm.tiktok.com/ZSabc/"
    photo = "https://www.tiktok.com/@creator/photo/123456789"
    page = metadata(photo)
    page.media_type = MediaType.ARTICLE
    page.canonical_url = photo
    page.source_url = short
    dispatcher._og.parse.side_effect = None
    dispatcher._og.parse.return_value = page
    metrics = metadata(photo, counts=True)
    metrics.media_type = MediaType.ARTICLE
    dispatcher._browser.parse_metrics.side_effect = None
    dispatcher._browser.parse_metrics.return_value = metrics
    result = await dispatcher.parse_card(short)
    assert result.metadata.media_type == MediaType.ARTICLE
    assert result.metadata.canonical_url == photo
    assert not missing_card_metrics(result.metadata)
    dispatcher._og.parse.assert_awaited_once_with(short)
    assert dispatcher._browser.parse_metrics.call_args.args[0] == photo


async def test_short_youtube_url_uses_watch_page_even_after_oembed_failure(
    dispatcher: Dispatcher,
) -> None:
    short = "https://www.youtube.com/shorts/dQw4w9WgXcQ"
    dispatcher._oembed.parse.side_effect = ParserError(short, "oembed failed")
    dispatcher._og.parse.side_effect = None
    dispatcher._og.parse.return_value = metadata(YT, counts=True)
    result = await dispatcher.parse_card(short)
    assert result.metadata.source_url == short
    assert not missing_card_metrics(result.metadata)
    dispatcher._og.parse.assert_awaited_once_with(YT)
    dispatcher._browser.parse_metrics.assert_not_called()
