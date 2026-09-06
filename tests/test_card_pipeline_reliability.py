from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import Settings
from src.listener import MessageEvent
from src.parsers.base import CardParseResult, CardStatus, DownloadCandidate, LinkMetadata, MediaType
from src.pipeline import Pipeline


def event(url: str) -> MessageEvent:
    return MessageEvent(message_id="card-test", chat_id="chat-test", sender_id="sender-test",
                        chat_type="group", timestamp_utc=0, message_type="text",
                        content='{"text":"' + url + '"}', mentions=[])


def pipeline_for(result: CardParseResult) -> Pipeline:
    pipeline = Pipeline(Settings(), MagicMock(), archive=MagicMock())
    pipeline._typing_sender.hold = MagicMock()
    pipeline._dispatcher.parse_card = AsyncMock(return_value=result)
    pipeline._dispatcher.invalidate_card = MagicMock()
    pipeline._dispatcher.prepare_video = AsyncMock()
    pipeline._translator.translate_metadata = AsyncMock()
    pipeline._sender.send = AsyncMock(return_value=True)
    pipeline._try_send_video = AsyncMock()
    return pipeline


@pytest.mark.asyncio
async def test_partial_real_content_archives_but_failure_placeholder_does_not() -> None:
    url = "https://www.tiktok.com/@creator/video/12345"
    partial = CardParseResult(LinkMetadata(source_url=url, title="Real caption", platform="tiktok"),
                              CardStatus.PARTIAL, has_content=True)
    pipeline = pipeline_for(partial)
    await pipeline._process_url(url, event(url), False, False)
    assert pipeline._archive.enqueue.call_args.args[0].partial is True
    pipeline._dispatcher.prepare_video.assert_not_awaited()
    pipeline._sender.send.assert_awaited_once()

    empty = CardParseResult(LinkMetadata(source_url=url, title="tiktok.com", platform="tiktok"),
                            CardStatus.UNAVAILABLE, reason="challenge")
    pipeline = pipeline_for(empty)
    await pipeline._process_url(url, event(url), False, False)
    pipeline._archive.enqueue.assert_not_called()
    pipeline._try_send_video.assert_not_awaited()
    assert "平台要求验证" in pipeline._sender.send.call_args.args[0]


@pytest.mark.asyncio
async def test_cover_upload_failure_invalidates_complete_metadata_cache(monkeypatch) -> None:
    url = "https://www.douyin.com/note/12345"
    result = CardParseResult(LinkMetadata(source_url=url, title="真实图文", platform="douyin",
                                         cover_url="https://cdn.example.com/signed?token=value",
                                         has_visual=True), CardStatus.COMPLETE, has_content=True)
    pipeline = pipeline_for(result)
    monkeypatch.setattr("src.pipeline.upload_cover", AsyncMock(return_value=None))
    await pipeline._process_url(url, event(url), False, False)
    pipeline._dispatcher.invalidate_card.assert_called_once_with(url)
    assert pipeline._archive.enqueue.call_args.args[0].partial is True
    assert "封面暂未获取" in pipeline._sender.send.call_args.args[0]
    pipeline._try_send_video.assert_not_awaited()


@pytest.mark.asyncio
async def test_captionless_image_is_not_archived_when_its_only_content_cannot_be_sent(
    monkeypatch,
) -> None:
    url = "https://www.douyin.com/note/12345"
    result = CardParseResult(
        LinkMetadata(source_url=url, platform="douyin", content_verified=True,
                     cover_url="https://cdn.example.com/image", has_visual=True),
        CardStatus.COMPLETE, has_content=True,
    )
    pipeline = pipeline_for(result)
    monkeypatch.setattr("src.pipeline.upload_cover", AsyncMock(return_value=None))
    await pipeline._process_url(url, event(url), False, False)
    pipeline._archive.enqueue.assert_not_called()
    assert result.status == CardStatus.UNAVAILABLE
    assert "原内容图片暂未获取" in pipeline._sender.send.call_args.args[0]


@pytest.mark.asyncio
async def test_translation_timeout_keeps_real_original_and_releases_work() -> None:
    url = "https://x.com/creator/status/12345"
    meta = LinkMetadata(source_url=url, title="Real post", platform="x", has_visual=False)
    pipeline = pipeline_for(CardParseResult(meta, CardStatus.COMPLETE, has_content=True))
    pipeline._settings.card_enrichment_timeout = 0.01
    stopped = asyncio.Event()

    async def slow_translate(meta):
        try:
            await asyncio.sleep(60)
        finally:
            stopped.set()

    pipeline._translator.translate_metadata = slow_translate
    start = time.monotonic()
    await pipeline._process_url(url, event(url), False, False)
    assert time.monotonic() - start < 0.5
    assert stopped.is_set()
    assert "Real post" in pipeline._sender.send.call_args.args[0]
    pipeline._sender.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_media_is_prepared_before_duration_or_candidate_skip(monkeypatch) -> None:
    url = "https://www.youtube.com/watch?v=abcdefghijk"
    meta = LinkMetadata(source_url=url, platform="youtube", title="Actual video",
                        media_type=MediaType.VIDEO)
    prepared = LinkMetadata(source_url=url, platform="youtube", media_type=MediaType.VIDEO,
                            duration_seconds=12,
                            download_candidates=[DownloadCandidate("https://cdn.example.com/v")])
    pipeline = pipeline_for(CardParseResult(meta, CardStatus.COMPLETE, has_content=True))
    pipeline._dispatcher.prepare_video = AsyncMock(return_value=prepared)
    video = MagicMock()
    download = AsyncMock(return_value=video)
    monkeypatch.setattr("src.pipeline.download_video", download)
    pipeline._video_sender.send = AsyncMock()
    await pipeline._download_and_send_video(meta, event(url), None, notify_user=False,
                                           enforce_duration_limit=True)
    assert download.call_args.args[0] is prepared
    video.cleanup.assert_called_once()


@pytest.mark.asyncio
async def test_douyin_does_not_prepare_or_download_even_when_misconfigured(monkeypatch) -> None:
    url = "https://www.douyin.com/video/12345"
    meta = LinkMetadata(source_url=url, platform="douyin", media_type=MediaType.VIDEO)
    pipeline = pipeline_for(CardParseResult(meta))
    pipeline._settings.allowed_video_platforms.append("douyin")
    download = AsyncMock()
    monkeypatch.setattr("src.pipeline.download_video", download)
    await pipeline._download_and_send_video(meta, event(url), None, notify_user=False,
                                           enforce_duration_limit=False)
    pipeline._dispatcher.prepare_video.assert_not_awaited()
    download.assert_not_awaited()
