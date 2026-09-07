from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import Settings
from src.listener import MessageEvent
from src.media_downloader import VideoDownloadError, VideoSkipReason, explain_video_skip
from src.parsers.base import (
    CardParseResult,
    CardStatus,
    DownloadCandidate,
    LinkMetadata,
    MediaType,
    ParserError,
)
from src.pipeline import Pipeline
from src.platforms import detect_platform

YOUTUBE = "https://www.youtube.com/watch?v=abcdefghijk"
VIDEO_URLS = [
    YOUTUBE,
    "https://www.bilibili.com/video/BV15itB6gEYE",
    "https://www.tiktok.com/@creator/video/7545221251367767318",
    "https://www.instagram.com/reel/Dc4u-j8JUG8/",
]


def message(url: str) -> MessageEvent:
    return MessageEvent(
        message_id="video-recovery-message",
        chat_id="video-recovery-chat",
        sender_id="video-recovery-user",
        chat_type="group",
        timestamp_utc=0,
        message_type="text",
        content=json.dumps({"text": url}),
        mentions=[],
    )


def unavailable(url: str = YOUTUBE, reason: str = "timeout") -> CardParseResult:
    return CardParseResult(
        LinkMetadata(source_url=url, platform=detect_platform(url), media_type=MediaType.VIDEO),
        CardStatus.UNAVAILABLE,
        reason,
        has_content=False,
    )


def downloadable(url: str = YOUTUBE, duration: int = 30) -> LinkMetadata:
    return LinkMetadata(
        source_url=url,
        platform=detect_platform(url),
        title="Verified short video",
        canonical_url=url,
        media_type=MediaType.VIDEO,
        duration_seconds=duration,
        download_candidates=[DownloadCandidate("https://cdn.example.test/video.mp4")],
        content_verified=True,
    )


@dataclass
class VideoHarness:
    pipeline: Pipeline
    trace: list[str]
    parse_card: AsyncMock
    extract_media: AsyncMock
    download: AsyncMock
    card_sender: AsyncMock
    video_sender: AsyncMock
    text_sender: AsyncMock
    archive: MagicMock
    video: MagicMock


@pytest.fixture
async def harness(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[VideoHarness]:
    archive = MagicMock()
    pipeline = Pipeline(Settings(video_append_enabled=True, cookie_refresh_enabled=False),
                        MagicMock(), archive=archive)
    trace: list[str] = []
    video = MagicMock()

    @asynccontextmanager
    async def hold(message_id: str, *, label: str) -> AsyncIterator[None]:
        yield

    async def send_card(*args: object, **kwargs: object) -> bool:
        trace.append("card")
        return True

    async def extract_media(url: str) -> LinkMetadata:
        trace.append("metadata")
        return downloadable(url)

    async def download(
        meta: LinkMetadata, settings: Settings, *, enforce_duration_limit: bool = True,
    ) -> MagicMock:
        # Keep the real eligibility checks; mock only the download's external I/O.
        reason = explain_video_skip(meta, settings, enforce_duration_limit=enforce_duration_limit)
        if reason:
            raise VideoSkipReason(reason)
        trace.append("download")
        return video

    async def send_video(*args: object, **kwargs: object) -> bool:
        trace.append("video")
        return True

    card_parser = AsyncMock(return_value=unavailable())
    media_extractor = AsyncMock(side_effect=extract_media)
    downloader = AsyncMock(side_effect=download)
    card_sender = AsyncMock(side_effect=send_card)
    video_sender = AsyncMock(side_effect=send_video)
    text_sender = AsyncMock(return_value=True)
    pipeline._typing_sender.hold = hold
    pipeline._dispatcher.parse_card = card_parser
    pipeline._dispatcher._ytdlp.parse = media_extractor
    pipeline._sender.send = card_sender
    pipeline._video_sender.send = video_sender
    pipeline._text_sender.send = text_sender
    pipeline._translator.translate_metadata = AsyncMock()
    monkeypatch.setattr("src.pipeline.download_video", downloader)
    try:
        yield VideoHarness(pipeline, trace, card_parser, media_extractor, downloader, card_sender,
                           video_sender, text_sender, archive, video)
    finally:
        await pipeline._http.aclose()


@pytest.mark.parametrize("url", VIDEO_URLS)
@pytest.mark.parametrize("reason", ["timeout", "parse_error"])
async def test_failed_card_still_prepares_and_sends_supported_video_after_one_card(
    harness: VideoHarness, url: str, reason: str,
) -> None:
    harness.parse_card.return_value = unavailable(url, reason)

    await harness.pipeline._process_url(url, message(url), False, False)

    assert harness.trace == ["card", "metadata", "download", "video"]
    harness.card_sender.assert_awaited_once()
    harness.video_sender.assert_awaited_once()
    harness.text_sender.assert_not_awaited()
    harness.archive.enqueue.assert_not_called()
    harness.video.cleanup.assert_called_once()
    assert harness.download.call_args.kwargs["enforce_duration_limit"] is True


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("stage", ["metadata", "download"])
async def test_media_failure_is_logged_and_manual_download_gets_an_explicit_reply(
    harness: VideoHarness, caplog: pytest.LogCaptureFixture, manual: bool, stage: str,
) -> None:
    if stage == "metadata":
        harness.extract_media.side_effect = ParserError(YOUTUBE, "extractor unavailable")
    else:
        harness.download.side_effect = VideoDownloadError("download connection failed")
    caplog.set_level(logging.WARNING)

    await harness.pipeline._process_url(YOUTUBE, message(YOUTUBE), manual, manual)

    harness.card_sender.assert_awaited_once()
    harness.extract_media.assert_awaited_once()
    harness.video_sender.assert_not_awaited()
    harness.archive.enqueue.assert_not_called()
    assert "video download failed" in caplog.text
    if manual:
        harness.text_sender.assert_awaited_once()
        assert "下载" in harness.text_sender.call_args.args[0]
    else:
        harness.text_sender.assert_not_awaited()


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("reason", ["challenge", "auth", "rate_limit", "deleted", "geo"])
async def test_explicit_access_restrictions_do_not_retry_media_automatically(
    harness: VideoHarness, reason: str, manual: bool,
) -> None:
    harness.parse_card.return_value = unavailable(reason=reason)

    await harness.pipeline._process_url(YOUTUBE, message(YOUTUBE), manual, manual)

    harness.card_sender.assert_awaited_once()
    harness.extract_media.assert_not_awaited()
    harness.video_sender.assert_not_awaited()
    harness.archive.enqueue.assert_not_called()
    if manual:
        harness.text_sender.assert_awaited_once()
        assert harness.text_sender.call_args.args[0]
    else:
        harness.text_sender.assert_not_awaited()


@pytest.mark.parametrize("case", ["article", "unsupported", "douyin", "card_send_failed"])
async def test_recovery_preserves_nonvideo_platform_and_card_delivery_guards(
    harness: VideoHarness, case: str,
) -> None:
    url = YOUTUBE
    result = unavailable()
    if case == "article":
        url = "https://www.instagram.com/reel/Dc4u-j8JUG8/"
        result = CardParseResult(
            LinkMetadata(source_url=url, platform="instagram", title="Verified photo post",
                         media_type=MediaType.ARTICLE, content_verified=True, has_visual=False),
            CardStatus.COMPLETE,
            has_content=True,
        )
    elif case == "unsupported":
        result.status = CardStatus.UNSUPPORTED
        result.reason = "unsupported"
    elif case == "douyin":
        url = "https://www.douyin.com/video/7668528121813954545"
        result = unavailable(url)
    else:
        harness.card_sender.side_effect = None
        harness.card_sender.return_value = False
    harness.parse_card.return_value = result

    await harness.pipeline._process_url(url, message(url), False, False)

    harness.card_sender.assert_awaited_once()
    harness.extract_media.assert_not_awaited()
    harness.video_sender.assert_not_awaited()
    assert "download" not in harness.trace
    if case != "article":
        harness.archive.enqueue.assert_not_called()


@pytest.mark.parametrize("known_before_prepare", [False, True])
async def test_recovered_video_still_obeys_automatic_duration_limit(
    harness: VideoHarness, known_before_prepare: bool,
) -> None:
    result = unavailable()
    harness.extract_media.side_effect = None
    harness.extract_media.return_value = downloadable(duration=181)
    if known_before_prepare:
        result.metadata.duration_seconds = 181
    harness.parse_card.return_value = result

    await harness.pipeline._process_url(YOUTUBE, message(YOUTUBE), False, False)

    harness.card_sender.assert_awaited_once()
    harness.video_sender.assert_not_awaited()
    harness.archive.enqueue.assert_not_called()
    assert "download" not in harness.trace
    if known_before_prepare:
        harness.extract_media.assert_not_awaited()
    else:
        harness.extract_media.assert_awaited_once()


async def test_manual_recovery_keeps_existing_duration_limit_bypass(harness: VideoHarness) -> None:
    harness.extract_media.side_effect = None
    harness.extract_media.return_value = downloadable(duration=181)

    await harness.pipeline._process_url(YOUTUBE, message(YOUTUBE), True, True)

    harness.card_sender.assert_awaited_once()
    harness.video_sender.assert_awaited_once()
    assert harness.download.call_args.kwargs["enforce_duration_limit"] is False
    harness.archive.enqueue.assert_not_called()


async def test_outer_card_timeout_keeps_video_intent_for_independent_media_preparation(
    harness: VideoHarness,
) -> None:
    cancelled = asyncio.Event()

    async def slow_card(url: str) -> CardParseResult:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    harness.pipeline._settings.card_prepare_timeout = 0.01
    harness.parse_card.side_effect = slow_card

    await asyncio.wait_for(
        harness.pipeline._process_url(YOUTUBE, message(YOUTUBE), False, False), timeout=1,
    )

    assert cancelled.is_set()
    assert harness.trace == ["card", "metadata", "download", "video"]
    harness.card_sender.assert_awaited_once()
    harness.archive.enqueue.assert_not_called()


async def test_outer_card_exception_keeps_video_intent_for_independent_media_preparation(
    harness: VideoHarness,
) -> None:
    harness.parse_card.side_effect = ValueError("changed upstream response")

    await harness.pipeline._process_url(YOUTUBE, message(YOUTUBE), False, False)

    assert harness.trace == ["card", "metadata", "download", "video"]
    harness.card_sender.assert_awaited_once()
    harness.archive.enqueue.assert_not_called()
