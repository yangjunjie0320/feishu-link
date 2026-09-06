import asyncio
import json
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

from src.card_metadata import CardSourceError
from src.config import Settings
from src.dispatch import Dispatcher
from src.listener import MessageEvent
from src.parsers.base import CardStatus, DownloadCandidate, LinkMetadata, MediaType, ParserError
from src.parsers.x_oembed import XOEmbedParser
from src.pipeline import Pipeline

YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
INSTAGRAM = "https://www.instagram.com/reel/abc123/"
X = "https://x.com/author/status/123"


@pytest.fixture
async def dispatcher() -> AsyncIterator[Dispatcher]:
    async with httpx.AsyncClient() as client:
        result = Dispatcher(Settings(cookie_refresh_enabled=False), client)
        result._ytdlp = AsyncMock()
        result._og = AsyncMock()
        result._og.parse.side_effect = ParserError(X, "page failed")
        result._og.parse_public.side_effect = ParserError(X, "public page failed")
        result._browser = AsyncMock()
        result._browser.parse.side_effect = ParserError(X, "browser failed")
        yield result


def video(url: str, platform: str = "youtube") -> LinkMetadata:
    return LinkMetadata(source_url=url, title="Real title", platform=platform,
                        cover_url="https://cdn.example/cover.jpg", media_type=MediaType.VIDEO,
                        content_verified=True)


async def test_card_uses_oembed_without_media_extraction_or_browser(dispatcher: Dispatcher) -> None:
    dispatcher._oembed = AsyncMock()
    dispatcher._oembed.parse.return_value = video(YOUTUBE)
    result = await dispatcher.parse_card(YOUTUBE)
    assert result.status == CardStatus.COMPLETE
    assert result.metadata.media_type == MediaType.VIDEO
    dispatcher._ytdlp.parse.assert_not_called()
    dispatcher._browser.parse.assert_not_called()
    dispatcher._og.parse.assert_not_called()


async def test_youtube_independent_page_can_recover_bad_oembed(dispatcher: Dispatcher) -> None:
    dispatcher._oembed = AsyncMock()
    dispatcher._oembed.parse.side_effect = ParserError(YOUTUBE, "invalid JSON")
    dispatcher._og.parse.return_value = video(YOUTUBE)
    dispatcher._og.parse.side_effect = None
    result = await dispatcher.parse_card(YOUTUBE)
    assert result.status == CardStatus.COMPLETE
    assert result.sources == ["page"]
    dispatcher._ytdlp.parse.assert_not_called()


async def test_instagram_reel_uses_caption_even_when_source_has_no_cover(
    dispatcher: Dispatcher,
) -> None:
    dispatcher._instagram_media_info = AsyncMock()
    dispatcher._instagram_media_info.parse.return_value = LinkMetadata(
        source_url=INSTAGRAM, title="Post by creator", description="Actual caption",
        platform="instagram", media_type=MediaType.VIDEO, has_visual=True, content_verified=True,
    )
    result = await dispatcher.parse_card(INSTAGRAM)
    assert result.status == CardStatus.PARTIAL
    assert result.metadata.description == "Actual caption"
    assert result.has_content
    dispatcher._ytdlp.parse.assert_not_called()


async def test_x_graphql_runs_even_when_public_endpoint_fails(dispatcher: Dispatcher) -> None:
    dispatcher._x_oembed = AsyncMock()
    dispatcher._x_oembed.parse.side_effect = ParserError(X, "public endpoint failed")
    dispatcher._x_graphql = AsyncMock()
    dispatcher._x_graphql.parse.return_value = LinkMetadata(
        source_url=X, description="Real plain text", platform="x",
        has_visual=False, content_verified=True, media_type=MediaType.ARTICLE,
    )
    result = await dispatcher.parse_card(X)
    assert result.status == CardStatus.COMPLETE
    assert result.metadata.media_type == MediaType.ARTICLE
    dispatcher._og.parse.assert_not_called()


async def test_browser_fills_cover_without_overwriting_http_caption(dispatcher: Dispatcher) -> None:
    dispatcher._x_oembed = AsyncMock()
    dispatcher._x_oembed.parse.return_value = LinkMetadata(
        source_url=X, description="Original caption", platform="x", content_verified=True,
    )
    dispatcher._x_graphql = AsyncMock()
    dispatcher._x_graphql.parse.side_effect = ParserError(X, "no cookie")
    dispatcher._browser.parse.side_effect = None
    dispatcher._browser.parse.return_value = video(X, "x")
    result = await dispatcher.parse_card(X)
    assert result.status == CardStatus.COMPLETE
    assert result.metadata.description == "Original caption"
    assert result.metadata.cover_url
    assert result.sources == ["x_oembed", "browser"]


async def test_placeholder_page_is_unavailable_not_a_successful_domain_card(
    dispatcher: Dispatcher,
) -> None:
    dispatcher._oembed = AsyncMock()
    dispatcher._oembed.parse.side_effect = ParserError(YOUTUBE, "restricted")
    dispatcher._og.parse.side_effect = None
    dispatcher._og.parse.return_value = LinkMetadata(source_url=YOUTUBE, title="youtube.com")
    result = await dispatcher.parse_card(YOUTUBE)
    assert result.status == CardStatus.UNAVAILABLE
    assert not result.has_content


async def test_timeout_keeps_fields_and_includes_admission_queue(dispatcher: Dispatcher) -> None:
    dispatcher._settings.card_parse_timeout = 0.03
    dispatcher._oembed = AsyncMock()
    dispatcher._oembed.parse.return_value = LinkMetadata(source_url=YOUTUBE, title="Real title")
    hanging = asyncio.Event()

    async def hang(url: str) -> LinkMetadata:
        await hanging.wait()
        raise AssertionError("not released")

    dispatcher._og.parse.side_effect = hang
    result = await dispatcher.parse_card(YOUTUBE)
    assert result.status == CardStatus.PARTIAL
    assert result.metadata.title == "Real title"
    assert result.reason == "timeout"

    await dispatcher._card_slots.acquire()
    await dispatcher._card_slots.acquire()
    await dispatcher._card_slots.acquire()
    await dispatcher._card_slots.acquire()
    try:
        queued = await dispatcher.parse_card("https://youtu.be/abcdefghijk")
    finally:
        for _ in range(4):
            dispatcher._card_slots.release()
    assert queued.status == CardStatus.UNAVAILABLE
    assert queued.reason == "timeout"


async def test_parse_limits_global_and_platform_concurrency() -> None:
    active = 0
    per_platform: dict[str, int] = {}
    peak = 0
    peaks: dict[str, int] = {}

    async def parse(url: str) -> LinkMetadata:
        nonlocal active, peak
        platform = "youtube" if "youtube" in url else "x"
        active += 1
        per_platform[platform] = per_platform.get(platform, 0) + 1
        peak = max(peak, active)
        peaks[platform] = max(peaks.get(platform, 0), per_platform[platform])
        await asyncio.sleep(0.01)
        active -= 1
        per_platform[platform] -= 1
        return video(url, platform)

    async with httpx.AsyncClient() as client:
        dispatcher = Dispatcher(Settings(cookie_refresh_enabled=False), client)
        dispatcher._oembed = AsyncMock()
        dispatcher._oembed.parse.side_effect = parse
        dispatcher._x_oembed = AsyncMock()
        dispatcher._x_oembed.parse.side_effect = parse
        urls = [f"https://www.youtube.com/watch?v=abcdefghij{i}" for i in range(6)]
        urls += [f"https://x.com/a/status/{i}" for i in range(6)]
        results = await asyncio.gather(*(dispatcher.parse_card(url) for url in urls))
    assert all(result.status == CardStatus.COMPLETE for result in results)
    assert peak == 4
    assert peaks == {"youtube": 2, "x": 2}


async def test_network_retry_budget_shared_across_sources(dispatcher: Dispatcher) -> None:
    dispatcher._oembed = AsyncMock()
    dispatcher._oembed.parse.side_effect = CardSourceError(YOUTUBE, "HTTP 503", kind="network")
    dispatcher._og.parse.side_effect = CardSourceError(YOUTUBE, "HTTP 502", kind="network")
    result = await dispatcher.parse_card(YOUTUBE)
    assert result.status == CardStatus.UNAVAILABLE
    assert dispatcher._oembed.parse.await_count == 2
    assert dispatcher._og.parse.await_count == 1


async def test_auth_refresh_once_but_rate_limit_does_not_refresh(
    dispatcher: Dispatcher, monkeypatch: pytest.MonkeyPatch,
) -> None:
    refresh = AsyncMock(return_value=True)
    monkeypatch.setattr("src.dispatch.force_refresh", refresh)
    dispatcher._instagram_media_info = AsyncMock()
    dispatcher._instagram_media_info.parse.side_effect = [
        CardSourceError(INSTAGRAM, "HTTP 401", kind="auth"), video(INSTAGRAM, "instagram"),
    ]
    assert (await dispatcher.parse_card(INSTAGRAM)).status == CardStatus.COMPLETE
    refresh.assert_awaited_once()
    dispatcher.invalidate_card(INSTAGRAM)
    dispatcher._instagram_media_info.parse.side_effect = CardSourceError(
        INSTAGRAM, "HTTP 429", kind="rate_limit",
    )
    await dispatcher.parse_card(INSTAGRAM)
    assert refresh.await_count == 1


async def test_rate_limit_prevents_other_platform_sources_within_retry_after(
    dispatcher: Dispatcher,
) -> None:
    dispatcher._x_oembed = AsyncMock()
    dispatcher._x_oembed.parse.return_value = LinkMetadata(
        source_url=X, platform="x", description="Already obtained text",
    )
    dispatcher._x_graphql = AsyncMock()
    dispatcher._x_graphql.parse.side_effect = CardSourceError(
        X, "HTTP 429", kind="rate_limit", retry_after=120,
    )
    result = await dispatcher.parse_card(X)
    assert result.status == CardStatus.PARTIAL
    assert result.reason == "rate_limit"
    assert result.metadata.description == "Already obtained text"
    dispatcher._og.parse.assert_not_called()
    dispatcher._browser.parse.assert_not_called()
    later = await dispatcher.parse_card("https://x.com/author/status/456")
    assert later.reason == "rate_limit"
    assert dispatcher._x_oembed.parse.await_count == 1


async def test_short_retry_after_delays_next_source_then_can_complete(
    dispatcher: Dispatcher,
) -> None:
    loop = asyncio.get_running_loop()
    received = loop.time()
    dispatcher._oembed = AsyncMock()
    dispatcher._oembed.parse.side_effect = CardSourceError(
        YOUTUBE, "HTTP 429", kind="rate_limit", retry_after=0.02,
    )

    async def page(url: str) -> LinkMetadata:
        assert loop.time() - received >= 0.02
        return video(url)

    dispatcher._og.parse.side_effect = page
    assert (await dispatcher.parse_card(YOUTUBE)).status == CardStatus.COMPLETE


async def test_bilibili_keeps_existing_metadata_source(dispatcher: Dispatcher) -> None:
    url = "https://www.bilibili.com/video/BV1234567890"
    dispatcher._ytdlp.parse.return_value = video(url, "bilibili")
    result = await dispatcher.parse_card(url)
    assert result.status == CardStatus.COMPLETE
    assert result.sources == ["bilibili_metadata"]
    dispatcher._og.parse.assert_not_called()


async def test_tiktok_empty_oembed_caption_continues_to_page(dispatcher: Dispatcher) -> None:
    url = "https://www.tiktok.com/@creator/video/123"
    dispatcher._oembed = AsyncMock()
    dispatcher._oembed.parse.return_value = LinkMetadata(
        source_url=url, platform="tiktok", cover_url="https://cdn.example/cover.jpg",
        media_type=MediaType.VIDEO, content_verified=True,
    )
    dispatcher._og.parse.side_effect = None
    dispatcher._og.parse.return_value = LinkMetadata(
        source_url=url, platform="tiktok", description="Real caption from the page",
        media_type=MediaType.VIDEO, content_verified=True,
    )
    result = await dispatcher.parse_card(url)
    assert result.status == CardStatus.COMPLETE
    assert result.sources == ["tiktok_oembed", "page"]
    assert result.metadata.description == "Real caption from the page"


@respx.mock
@pytest.mark.parametrize("manual", [False, True])
async def test_x_oembed_only_keeps_unknown_and_allows_auto_or_manual_video_preparation(
    dispatcher: Dispatcher, manual: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    respx.get("https://publish.twitter.com/oembed").mock(return_value=httpx.Response(200, json={
        "author_name": "Author", "html": "<blockquote><p>Real post text</p></blockquote>",
    }))
    dispatcher._x_graphql = AsyncMock()
    dispatcher._x_graphql.parse.side_effect = ParserError(X, "no cookie")
    assert isinstance(dispatcher._x_oembed, XOEmbedParser)
    result = await dispatcher.parse_card(X)
    assert result.status == CardStatus.PARTIAL
    assert result.metadata.media_type == MediaType.UNKNOWN
    dispatcher._ytdlp.parse.return_value = LinkMetadata(
        source_url=X, platform="x", media_type=MediaType.VIDEO, duration_seconds=120,
        download_candidates=[DownloadCandidate("https://cdn.example/video.mp4")],
    )
    pipeline = Pipeline(Settings(), MagicMock())
    pipeline._dispatcher = dispatcher
    pipeline._video_sender.send = AsyncMock(return_value=True)
    downloaded = MagicMock()
    download = AsyncMock(return_value=downloaded)
    monkeypatch.setattr("src.pipeline.download_video", download)
    event = MessageEvent(message_id="test", chat_id="chat", sender_id="sender", chat_type="group",
                         timestamp_utc=0, message_type="text", content=json.dumps({"text": X}),
                         mentions=[])
    try:
        await pipeline._download_and_send_video(result.metadata, event, None, notify_user=manual,
                                               enforce_duration_limit=not manual)
    finally:
        await pipeline._http.aclose()
    dispatcher._ytdlp.parse.assert_awaited_once()
    assert download.call_args.args[0].media_type == MediaType.VIDEO
    assert download.call_args.kwargs["enforce_duration_limit"] is not manual


async def test_prepare_video_copies_card_and_supplies_download_fields(
    dispatcher: Dispatcher,
) -> None:
    meta = video(YOUTUBE)
    dispatcher._ytdlp.parse.return_value = LinkMetadata(
        source_url=YOUTUBE, title="Other extractor title", duration_seconds=120,
        download_candidates=[DownloadCandidate("https://cdn.example/video.mp4")],
        media_type=MediaType.VIDEO,
    )
    prepared = await dispatcher.prepare_video(meta)
    assert prepared.title == "Real title"
    assert prepared.duration_seconds == 120
    assert prepared.download_candidates
    assert meta.download_candidates == []


@pytest.mark.parametrize("url", [
    "https://instagram.com/author/", "https://x.com/author", "https://youtube.com/@author",
])
async def test_profiles_are_unsupported_content_links(dispatcher: Dispatcher, url: str) -> None:
    assert (await dispatcher.parse_card(url)).status == CardStatus.UNSUPPORTED
    dispatcher._browser.parse.assert_not_called()
