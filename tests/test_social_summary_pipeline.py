from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from src.archive_store import BibiLinkUpdate
from src.bibi_client import BibiAPIError
from src.bibi_models import SummaryResult, Usage
from src.config import Settings
from src.listener import CardActionEvent, MessageEvent
from src.parsers.base import CardParseResult, CardStatus, LinkMetadata, MediaType
from src.pipeline import Pipeline, _summary_failure_message

SHORT = "https://v.douyin.com/7JK2hU2p0QI/"
DOUYIN = "https://www.douyin.com/video/7668528121813954545"
TIKTOK = "https://www.tiktok.com/@dylan.page/video/7681700718713048342"


def message(url: str, message_id: str = "share", prompt: str = "") -> MessageEvent:
    return MessageEvent(
        message_id=message_id, chat_id="test-chat", sender_id="test-user",
        chat_type="group", timestamp_utc=0, message_type="text", mentions=[],
        content=json.dumps({"text": f"{prompt} {url}".strip()}),
    )


def parsed(url: str = SHORT, target: str = DOUYIN, platform: str = "douyin") -> CardParseResult:
    return CardParseResult(
        LinkMetadata(source_url=url, canonical_url=target, platform=platform,
                     title="真实视频主题", description="这是作品的真实文案",
                     media_type=MediaType.VIDEO, content_verified=True),
        CardStatus.COMPLETE, has_content=True,
    )


def summary() -> SummaryResult:
    return SummaryResult(
        content="视频的实际总结", model="bibigpt-web", from_cache=False,
        usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
        video_url=DOUYIN, content_id="verified-content-id",
    )


@pytest_asyncio.fixture
async def pipeline():
    archive = MagicMock()
    archive.find_bibigpt_content_id = AsyncMock(return_value="")
    instance = Pipeline(Settings(), MagicMock(), archive=archive)
    instance._typing_sender.hold = MagicMock()
    instance._dispatcher.parse_card = AsyncMock(return_value=parsed())
    instance._dispatcher.prepare_video = AsyncMock()
    instance._sender.send = AsyncMock(return_value=True)
    instance._text_sender.send = AsyncMock(return_value=True)
    instance._translator.ensure_chinese_markdown_summary = AsyncMock(
        side_effect=lambda text, **kwargs: text
    )
    instance._bibi_client.summarize = AsyncMock(return_value=summary())
    instance._bibi_client.summarize_cached = AsyncMock(return_value=None)
    instance._try_send_bibigpt_chapter_summary = AsyncMock()
    instance._try_send_video = AsyncMock()
    try:
        yield instance
    finally:
        await instance._http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("url,target,platform", [
    (SHORT, DOUYIN, "douyin"), (TIKTOK + "?is_from_webapp=1", TIKTOK, "tiktok"),
])
async def test_mention_sends_card_before_summary_without_media_or_reparse(
    pipeline, url, target, platform,
) -> None:
    result = parsed(url, target, platform)
    pipeline._prepare_card = AsyncMock(return_value=(result, None))
    order = []

    async def send(card, chat_id, message_id):
        order.append("summary" if "BibiGPT 总结" in card else "card")
        return True

    pipeline._sender.send = AsyncMock(side_effect=send)
    await pipeline._process_url(url, message(url, prompt="请用三句话说明"), True, False)

    assert order == ["card", "summary"]
    pipeline._bibi_client.summarize.assert_awaited_once_with(target, prompt="请用三句话说明")
    pipeline._dispatcher.parse_card.assert_not_awaited()
    pipeline._dispatcher.prepare_video.assert_not_awaited()
    pipeline._try_send_video.assert_not_awaited()
    pipeline._try_send_bibigpt_chapter_summary.assert_awaited_once()
    update = pipeline._archive.enqueue.call_args.args[0]
    assert isinstance(update, BibiLinkUpdate)
    assert update.url == target
    assert url in pipeline._sender.send.call_args.args[0]


@pytest.mark.asyncio
async def test_share_without_mention_does_not_automatically_summarize(pipeline) -> None:
    pipeline._prepare_card = AsyncMock(return_value=(parsed(), None))
    await pipeline._process_url(SHORT, message(SHORT), False, False)
    pipeline._bibi_client.summarize.assert_not_awaited()
    pipeline._try_send_video.assert_not_awaited()
    pipeline._sender.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_old_callback_resolves_short_link_and_checks_actual_media(pipeline) -> None:
    event = CardActionEvent(action="summarize_video", source_url=SHORT,
                            message_id="old-card", chat_id="test-chat", operator_open_id="user")
    await pipeline._handle_card_action(event)
    pipeline._dispatcher.parse_card.assert_awaited_once_with(SHORT)
    pipeline._bibi_client.summarize.assert_awaited_once_with(DOUYIN, prompt=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("url,target,platform", [
    (SHORT, "https://www.douyin.com/note/7658101922843080169", "douyin"),
    ("https://vm.tiktok.com/share/", "https://www.tiktok.com/@creator/photo/12345", "tiktok"),
])
async def test_photo_cannot_bypass_summary_boundary_through_old_callback(
    pipeline, url, target, platform,
) -> None:
    result = parsed(url, target, platform)
    result.metadata.media_type = MediaType.ARTICLE
    pipeline._dispatcher.parse_card = AsyncMock(return_value=result)
    await pipeline._try_send_bibigpt_summary(url, message(url))
    pipeline._bibi_client.summarize.assert_not_awaited()
    assert "图文" in pipeline._text_sender.send.call_args.args[0]
    assert pipeline._summary_targets == {}
    assert pipeline._active_summary_messages == set()


@pytest.mark.asyncio
async def test_empty_or_cross_platform_resolution_never_reaches_bibigpt(pipeline) -> None:
    pipeline._dispatcher.parse_card = AsyncMock(return_value=CardParseResult(
        LinkMetadata(source_url=SHORT), reason="challenge"
    ))
    await pipeline._try_send_bibigpt_summary(SHORT, message(SHORT))
    assert "验证" in pipeline._text_sender.send.call_args.args[0]
    pipeline._dispatcher.parse_card = AsyncMock(return_value=parsed(SHORT, TIKTOK, "tiktok"))
    await pipeline._try_send_bibigpt_summary(SHORT, message(SHORT, "second"))
    pipeline._bibi_client.summarize.assert_not_awaited()
    assert "视频链接" in pipeline._text_sender.send.call_args.args[0]


@pytest.mark.asyncio
async def test_short_and_canonical_shares_share_generation_and_keep_separate_replies(
    pipeline,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(url, prompt=None):
        entered.set()
        await release.wait()
        return summary()

    pipeline._bibi_client.summarize = AsyncMock(side_effect=generate)
    first = asyncio.create_task(pipeline._try_send_bibigpt_summary(SHORT, message(SHORT, "first")))
    await entered.wait()
    second = asyncio.create_task(
        pipeline._try_send_bibigpt_summary(DOUYIN, message(DOUYIN, "second"))
    )
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)

    pipeline._bibi_client.summarize.assert_awaited_once_with(DOUYIN, prompt=None)
    assert {call.args[2] for call in pipeline._sender.send.await_args_list} == {"first", "second"}
    assert list(pipeline._recent_summaries) == [DOUYIN]
    assert pipeline._inflight_summaries == {}
    alias = DOUYIN + "?share_source=copy"
    await pipeline._try_send_bibigpt_summary(
        alias, message(alias, "third")
    )
    assert pipeline._bibi_client.summarize.await_count == 1


@pytest.mark.asyncio
async def test_failure_cooldown_is_shared_across_aliases(pipeline) -> None:
    pipeline._bibi_client.summarize = AsyncMock(side_effect=BibiAPIError(500, "Connection error"))
    await pipeline._try_send_bibigpt_summary(SHORT, message(SHORT))
    await pipeline._try_send_bibigpt_summary(DOUYIN, message(DOUYIN, "second"))
    assert pipeline._bibi_client.summarize.await_count == 1
    assert list(pipeline._summary_failures) == [DOUYIN]
    assert "冷却" in pipeline._text_sender.send.call_args.args[0]


@pytest.mark.asyncio
async def test_resolution_cancellation_releases_message_and_allows_retry(pipeline) -> None:
    entered = asyncio.Event()

    async def resolve(url):
        entered.set()
        await asyncio.Event().wait()

    pipeline._dispatcher.parse_card = AsyncMock(side_effect=resolve)
    task = asyncio.create_task(pipeline._try_send_bibigpt_summary(SHORT, message(SHORT)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pipeline._active_summary_messages == set()
    pipeline._dispatcher.parse_card = AsyncMock(return_value=parsed())
    await pipeline._try_send_bibigpt_summary(SHORT, message(SHORT))
    pipeline._bibi_client.summarize.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolution_timeout_does_not_generate_or_cache(pipeline) -> None:
    pipeline._settings.card_parse_timeout = 0.01

    async def resolve(url):
        await asyncio.Event().wait()

    pipeline._dispatcher.parse_card = AsyncMock(side_effect=resolve)
    await pipeline._try_send_bibigpt_summary(SHORT, message(SHORT))
    pipeline._bibi_client.summarize.assert_not_awaited()
    assert "确认超时" in pipeline._text_sender.send.call_args.args[0]
    assert pipeline._summary_targets == {}


@pytest.mark.asyncio
async def test_douyin_download_command_remains_disabled(pipeline) -> None:
    pipeline._prepare_card = AsyncMock(return_value=(parsed(), None))
    await pipeline._process_url(SHORT, message(SHORT, prompt="下载"), True, True)
    pipeline._bibi_client.summarize.assert_not_awaited()
    pipeline._try_send_video.assert_not_awaited()
    assert "暂不支持视频下载" in pipeline._text_sender.send.call_args.args[0]


def test_new_platform_risk_message_does_not_claim_bilibili() -> None:
    result = _summary_failure_message(BibiAPIError(500, "平台风控"))
    assert "风控" in result
    assert "B 站" not in result


@pytest.mark.asyncio
async def test_bilibili_part_selection_survives_page_canonical_and_stays_separate(pipeline) -> None:
    canonical = "https://www.bilibili.com/video/BV15itB6gEYE"
    part_two = canonical + "?p=2"
    meta = parsed(part_two, canonical, "bilibili").metadata
    assert pipeline._remember_summary_target(part_two, meta) == part_two
    assert await pipeline._resolve_summary_target(part_two) == part_two
    assert await pipeline._resolve_summary_target(canonical) == canonical
