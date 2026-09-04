import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.archive_store import BibiLinkUpdate, RemarkAppend
from src.bibi_client import AuthenticationError, BibiAPIError, TranscriptUnavailableError
from src.bibi_models import (
    ChapterSummaryFetchResult,
    ChapterSummarySection,
    SummaryResult,
    Usage,
)
from src.comment_analyzer import CommentAnalysisError
from src.config import Settings
from src.listener import CardActionEvent, MessageEvent
from src.parsers.base import LinkMetadata
from src.pipeline import (
    Pipeline,
    _comment_analysis_failure_message,
    _friendly_download_reason,
    _summary_failure_message,
)


class _NoopHold:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


def _summary_result() -> SummaryResult:
    return SummaryResult(
        content="- **总结**\n    - 内容",
        model="bibigpt-web",
        usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
        from_cache=False,
        video_url="https://youtu.be/abc123",
        content_id="content-123",
    )


def _chapter_section(
    *, title: str = "Original title", summary: str = "Original summary"
) -> ChapterSummarySection:
    return ChapterSummarySection(
        index=0,
        start_time=0.0,
        end_time=266.8,
        title=title,
        summary=summary,
    )


def _summary_event() -> CardActionEvent:
    return CardActionEvent(
        action="summarize_video",
        source_url="https://youtu.be/abc123",
        message_id="om_summary",
        chat_id="oc_chat",
        operator_open_id="ou_user",
    )


def _prepare_summary_pipeline() -> Pipeline:
    pipeline = Pipeline(Settings(), MagicMock())
    pipeline._typing_sender.hold = MagicMock(return_value=_NoopHold())  # type: ignore[method-assign]
    pipeline._translator.ensure_chinese_markdown_summary = AsyncMock(  # type: ignore[method-assign]
        return_value="- **总结**\n    - 内容"
    )
    return pipeline


def test_friendly_download_reason_for_duration_limit() -> None:
    assert (
        _friendly_download_reason("video duration exceeds limit: duration=181 limit=180")
        == "视频时长 181 秒, 超过自动追加限制 180 秒。"
    )


def test_friendly_download_reason_for_candidate_file_limit() -> None:
    assert (
        _friendly_download_reason("video filesize exceeds limit: size_mb=31.00 limit_mb=30")
        == "视频候选文件约 31.00 MB, 超过当前限制 30 MB。"
    )


def test_friendly_download_reason_for_downloaded_file_limit() -> None:
    assert (
        _friendly_download_reason("downloaded video too large: size_mb=32.12 limit_mb=30")
        == "下载后文件约 32.12 MB, 超过当前限制 30 MB。"
    )


def test_friendly_download_reason_for_non_video() -> None:
    assert _friendly_download_reason("not a video: media_type=article") == (
        "这个链接没有识别为可下载视频。"
    )


def test_comment_analysis_failure_message() -> None:
    assert _comment_analysis_failure_message(CommentAnalysisError("没有获取到评论内容。")) == (
        "评论区分析失败: 没有获取到评论内容。"
    )


def test_summary_failure_message_for_corrupt_bibigpt_cookie() -> None:
    message = _summary_failure_message(
        AuthenticationError(
            0,
            "BibiGPT auth cookie is corrupted or incomplete; please re-export aitodo.co cookies.",
        )
    )

    assert message == (
        "BibiGPT 总结失败: 本地 cookie 文件不完整或已损坏, "
        "请重新导出 aitodo.co cookies 后更新 cookies/bibigpt.txt。"
    )


def test_summary_failure_message_for_html_error_page() -> None:
    message = _summary_failure_message(
        BibiAPIError(403, "<!DOCTYPE html><html><head></head></html>")
    )

    assert message == (
        "BibiGPT 总结失败: 服务返回了 HTML 错误页 (HTTP 403), "
        "可能是登录态异常或服务端临时异常, 请稍后重试。"
    )


def test_summary_failure_message_for_missing_transcript() -> None:
    message = _summary_failure_message(
        TranscriptUnavailableError(200, "BibiGPT did not receive a transcript.")
    )

    assert message == (
        "BibiGPT 总结失败: 没有拿到这个视频的字幕或转录文本, "
        "可能是视频没有字幕、字幕被限制, 或转录抓取临时失败, 可稍后重试。"
    )


def test_summary_failure_message_for_quota_exhausted() -> None:
    body = '{"message":"Payment Required（余额不足啦）","data":{"code":"PAYMENT_REQUIRED"}}'
    message = _summary_failure_message(BibiAPIError(402, body))

    assert message == (
        "BibiGPT 总结失败: 账号额度不足或会员已过期, 需要为 aitodo.co 账号充值/续费后重试。"
    )


def test_summary_failure_message_for_rate_limit() -> None:
    assert _summary_failure_message(BibiAPIError(429, "Too Many Requests")) == (
        "BibiGPT 总结失败: 触发接口限流, 请稍后重试。"
    )


def test_summary_failure_message_for_transient_server_error() -> None:
    body = '[{"error":{"json":{"message":"Connection error.","data":{"httpStatus":500}}}}]'
    assert _summary_failure_message(BibiAPIError(500, body)) == (
        "BibiGPT 总结失败: BibiGPT 服务端暂时不稳定 (HTTP 500), 通常是临时故障, 请稍后重试。"
    )


def test_summary_failure_message_for_browser_failure_without_status() -> None:
    assert _summary_failure_message(BibiAPIError(0, "BibiGPT browser request timed out")) == (
        "BibiGPT 总结失败: BibiGPT 服务端暂时不稳定, 通常是临时故障, 请稍后重试。"
    )


def test_summary_failure_message_for_expired_login() -> None:
    assert _summary_failure_message(AuthenticationError(403, "forbidden")) == (
        "BibiGPT 总结失败: 登录态已失效, 需要在服务器上重新登录 aitodo.co 账号后重试。"
    )


@pytest.mark.asyncio
async def test_summary_sends_card_before_chapter_summary_card() -> None:
    pipeline = _prepare_summary_pipeline()
    summary = _summary_result()
    section = _chapter_section()
    formatted = _chapter_section(title="中文标题", summary="中文摘要")
    timeline: list[str] = []
    pipeline._bibi_client.summarize = AsyncMock(return_value=summary)  # type: ignore[method-assign]

    async def fetch_chapter_summary(
        result: SummaryResult,
    ) -> ChapterSummaryFetchResult:
        assert result is summary
        timeline.append("fetch")
        return ChapterSummaryFetchResult(
            introduction="Original introduction",
            sections=(section,),
            status="available",
            source="video.chapterSummary",
        )

    async def format_chapter_summary(
        introduction: str,
        sections,
        *,
        content_id: str = "",
    ):
        assert introduction == "Original introduction"
        assert sections == (section,)
        assert content_id == "content-123"
        timeline.append("format")
        return "中文总述", (formatted,)

    async def send(card_json: str, chat_id: str, message_id: str) -> bool:
        card = json.loads(card_json)
        title = card["header"]["title"]["content"]
        timeline.append("summary" if title == "BibiGPT 总结" else "chapter")
        assert chat_id == "oc_chat"
        assert message_id == "om_summary"
        return True

    pipeline._bibi_client.fetch_chapter_summary = fetch_chapter_summary  # type: ignore[method-assign]
    pipeline._translator.format_chapter_summary = format_chapter_summary  # type: ignore[method-assign]
    pipeline._sender.send = send  # type: ignore[method-assign]

    await pipeline._try_send_bibigpt_summary(summary.video_url, _summary_event())

    assert timeline == ["summary", "fetch", "format", "chapter"]


@pytest.mark.asyncio
async def test_summary_reports_unavailable_chapter_summary_without_formatting() -> None:
    pipeline = _prepare_summary_pipeline()
    summary = _summary_result()
    pipeline._bibi_client.summarize = AsyncMock(return_value=summary)  # type: ignore[method-assign]
    pipeline._bibi_client.fetch_chapter_summary = AsyncMock(  # type: ignore[method-assign]
        return_value=ChapterSummaryFetchResult(
            introduction="",
            sections=(),
            status="unavailable",
            source="none",
            reason="No chapter summary.",
        )
    )
    pipeline._translator.format_chapter_summary = AsyncMock()  # type: ignore[method-assign]
    pipeline._sender.send = AsyncMock(return_value=True)  # type: ignore[method-assign]
    pipeline._text_sender.send = AsyncMock(return_value=True)  # type: ignore[method-assign]

    await pipeline._try_send_bibigpt_summary(summary.video_url, _summary_event())

    pipeline._translator.format_chapter_summary.assert_not_called()
    pipeline._text_sender.send.assert_awaited_once_with(
        "BibiGPT 字幕总结暂不可用: 没有获取到章节总结。",
        "oc_chat",
        "om_summary",
    )


@pytest.mark.asyncio
async def test_summary_send_failure_does_not_lookup_chapter_summary() -> None:
    pipeline = _prepare_summary_pipeline()
    summary = _summary_result()
    pipeline._bibi_client.summarize = AsyncMock(return_value=summary)  # type: ignore[method-assign]
    pipeline._bibi_client.fetch_chapter_summary = AsyncMock()  # type: ignore[method-assign]
    pipeline._sender.send = AsyncMock(return_value=False)  # type: ignore[method-assign]
    pipeline._text_sender.send = AsyncMock(return_value=True)  # type: ignore[method-assign]

    await pipeline._try_send_bibigpt_summary(summary.video_url, _summary_event())

    pipeline._bibi_client.fetch_chapter_summary.assert_not_called()
    pipeline._text_sender.send.assert_not_called()


@pytest.mark.asyncio
async def test_summary_generation_failure_does_not_lookup_chapter_summary() -> None:
    pipeline = _prepare_summary_pipeline()
    pipeline._bibi_client.summarize = AsyncMock(  # type: ignore[method-assign]
        side_effect=BibiAPIError(500, "temporary failure")
    )
    pipeline._bibi_client.fetch_chapter_summary = AsyncMock()  # type: ignore[method-assign]
    pipeline._sender.send = AsyncMock(return_value=True)  # type: ignore[method-assign]
    pipeline._text_sender.send = AsyncMock(return_value=True)  # type: ignore[method-assign]

    await pipeline._try_send_bibigpt_summary(
        "https://youtu.be/abc123",
        _summary_event(),
    )

    pipeline._bibi_client.fetch_chapter_summary.assert_not_called()
    pipeline._sender.send.assert_not_called()
    pipeline._text_sender.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_chapter_summary_send_failure_stops_later_parts_and_reports_progress(
    monkeypatch,
) -> None:
    pipeline = _prepare_summary_pipeline()
    summary = _summary_result()
    section = _chapter_section()
    pipeline._bibi_client.summarize = AsyncMock(return_value=summary)  # type: ignore[method-assign]
    pipeline._bibi_client.fetch_chapter_summary = AsyncMock(  # type: ignore[method-assign]
        return_value=ChapterSummaryFetchResult(
            introduction="Overview",
            sections=(section,),
            status="available",
            source="video.chapterSummary",
        )
    )
    pipeline._translator.format_chapter_summary = AsyncMock(  # type: ignore[method-assign]
        return_value=("总述", (section,))
    )
    pipeline._sender.send = AsyncMock(side_effect=[True, True, False])  # type: ignore[method-assign]
    pipeline._text_sender.send = AsyncMock(return_value=True)  # type: ignore[method-assign]
    monkeypatch.setattr(
        "src.pipeline.build_chapter_summary_cards",
        lambda introduction, sections: ["chapter-card-1", "chapter-card-2", "chapter-card-3"],
    )
    sleep = AsyncMock()
    monkeypatch.setattr("src.pipeline.asyncio.sleep", sleep)

    await pipeline._try_send_bibigpt_summary(summary.video_url, _summary_event())

    assert pipeline._sender.send.await_count == 3
    sent_cards = [call.args[0] for call in pipeline._sender.send.await_args_list]
    assert sent_cards[1:] == ["chapter-card-1", "chapter-card-2"]
    for call in pipeline._sender.send.await_args_list:
        assert call.args[1:] == ("oc_chat", "om_summary")
    sleep.assert_awaited_once_with(0.25)
    pipeline._text_sender.send.assert_awaited_once_with(
        "BibiGPT 字幕总结发送不完整: 已发送 1/3 段, 可重试。",
        "oc_chat",
        "om_summary",
    )


@pytest.mark.asyncio
async def test_pipeline_silently_ignores_unsupported_platform_url() -> None:
    lark_client = MagicMock()
    pipeline = Pipeline(Settings(), lark_client)

    dispatcher_parse = AsyncMock()
    pipeline._dispatcher.parse = dispatcher_parse  # type: ignore[method-assign]

    sender_send = AsyncMock(return_value=True)
    pipeline._sender.send = sender_send  # type: ignore[method-assign]

    from src.listener import MessageEvent

    event = MessageEvent(
        message_id="msg_001",
        chat_id="chat_001",
        sender_id="u_001",
        chat_type="group",
        timestamp_utc=0,
        message_type="text",
        content='{"text":"https://www.example.com/some-article"}',
        mentions=[],
    )
    await pipeline._process_url("https://www.example.com/some-article", event, False, False)

    dispatcher_parse.assert_not_called()
    sender_send.assert_not_called()


def _link_message_event() -> MessageEvent:
    return MessageEvent(
        message_id="msg_arch",
        chat_id="oc_chat",
        sender_id="ou_sender",
        chat_type="group",
        timestamp_utc=0,
        message_type="text",
        content='{"text":"https://youtu.be/abc123"}',
        mentions=[],
    )


def _prepare_card_pipeline(archive) -> Pipeline:
    pipeline = Pipeline(Settings(), MagicMock(), archive=archive)
    pipeline._typing_sender.hold = MagicMock(return_value=_NoopHold())  # type: ignore[method-assign]
    meta = LinkMetadata(
        source_url="https://youtu.be/abc123",
        title="Original title",
        translated_title="翻译标题",
        canonical_url="https://www.youtube.com/watch?v=abc123",
        platform="youtube",
        channel="Some Channel",
        duration_seconds=90,
    )
    pipeline._dispatcher.parse = AsyncMock(return_value=meta)  # type: ignore[method-assign]
    pipeline._translator.translate_metadata = AsyncMock()  # type: ignore[method-assign]
    pipeline._sender.send = AsyncMock(return_value=True)  # type: ignore[method-assign]
    pipeline._try_send_video = AsyncMock()  # type: ignore[method-assign]
    return pipeline


async def test_process_url_enqueues_archive_entry_after_card_sent() -> None:
    archive = MagicMock()
    pipeline = _prepare_card_pipeline(archive)

    await pipeline._process_url("https://youtu.be/abc123", _link_message_event(), False, False)

    archive.enqueue.assert_called_once()
    entry = archive.enqueue.call_args.args[0]
    assert entry.title == "翻译标题"
    assert entry.url == "https://www.youtube.com/watch?v=abc123"
    assert entry.platform == "youtube"
    assert entry.sender_open_id == "ou_sender"
    assert entry.chat_id == "oc_chat"
    assert entry.chat_type == "group"


async def test_process_url_does_not_enqueue_when_card_send_fails() -> None:
    archive = MagicMock()
    pipeline = _prepare_card_pipeline(archive)
    pipeline._sender.send = AsyncMock(return_value=False)  # type: ignore[method-assign]

    await pipeline._process_url("https://youtu.be/abc123", _link_message_event(), False, False)

    archive.enqueue.assert_not_called()


async def test_process_url_without_archive_still_works() -> None:
    pipeline = _prepare_card_pipeline(None)

    await pipeline._process_url("https://youtu.be/abc123", _link_message_event(), False, False)

    pipeline._sender.send.assert_awaited_once()


def _multi_link_event(text: str, message_id: str = "msg_multi") -> MessageEvent:
    return MessageEvent(
        message_id=message_id,
        chat_id="oc_chat",
        sender_id="ou_sender",
        chat_type="group",
        timestamp_utc=0,
        message_type="text",
        content=json.dumps({"text": text}),
        mentions=[],
    )


async def test_handle_skips_message_with_multiple_urls() -> None:
    pipeline = Pipeline(Settings(), MagicMock())
    pipeline._process_url = AsyncMock()  # type: ignore[method-assign]

    event = _multi_link_event("https://youtu.be/abc123 https://youtu.be/def456")
    await pipeline.handle(event)

    pipeline._process_url.assert_not_called()


async def test_handle_processes_single_url_message() -> None:
    pipeline = Pipeline(Settings(), MagicMock())
    pipeline._process_url = AsyncMock()  # type: ignore[method-assign]

    event = _multi_link_event("https://youtu.be/abc123", message_id="msg_single")
    await pipeline.handle(event)

    pipeline._process_url.assert_awaited_once()


async def test_handle_processes_duplicate_url_message() -> None:
    pipeline = Pipeline(Settings(), MagicMock())
    pipeline._process_url = AsyncMock()  # type: ignore[method-assign]

    event = _multi_link_event("https://youtu.be/abc123 https://youtu.be/abc123", "msg_dup")
    await pipeline.handle(event)

    pipeline._process_url.assert_awaited_once()


def _reply_event(
    text: str = "讲得不错",
    *,
    message_id: str = "msg_reply",
    root_id: str = "",
    parent_id: str = "",
) -> MessageEvent:
    return MessageEvent(
        message_id=message_id,
        chat_id="oc_chat",
        sender_id="ou_replier",
        chat_type="group",
        timestamp_utc=0,
        message_type="text",
        content=json.dumps({"text": text}),
        mentions=[],
        root_id=root_id,
        parent_id=parent_id,
    )


def _message_fetch_response(items: list[dict], success: bool = True):
    from types import SimpleNamespace

    payload = json.dumps({"code": 0, "data": {"items": items}}).encode("utf-8")
    return SimpleNamespace(
        success=lambda: success,
        code=0 if success else 99991672,
        msg="ok" if success else "forbidden",
        raw=SimpleNamespace(content=payload),
    )


async def test_reply_remark_enqueued_via_in_process_map() -> None:
    archive = MagicMock()
    pipeline = _prepare_card_pipeline(archive)
    await pipeline._process_url("https://youtu.be/abc123", _link_message_event(), False, False)

    await pipeline.handle(_reply_event(root_id="msg_arch"))

    remark = archive.enqueue.call_args_list[-1].args[0]
    assert isinstance(remark, RemarkAppend)
    assert remark.url == "https://www.youtube.com/watch?v=abc123"
    assert remark.text == "讲得不错"
    assert remark.sender_open_id == "ou_replier"
    assert remark.chat_id == "oc_chat"
    assert remark.chat_type == "group"
    assert remark.message_id == "msg_reply"


async def test_reply_to_card_resolves_via_root_id_map_too() -> None:
    archive = MagicMock()
    pipeline = _prepare_card_pipeline(archive)
    await pipeline._process_url("https://youtu.be/abc123", _link_message_event(), False, False)

    # Quote-replying the bot's card: parent_id is the card message, root_id
    # still points at the original link message recorded in the map.
    await pipeline.handle(_reply_event(root_id="msg_arch", parent_id="om_card"))

    remark = archive.enqueue.call_args_list[-1].args[0]
    assert isinstance(remark, RemarkAppend)
    assert remark.url == "https://www.youtube.com/watch?v=abc123"


async def test_reply_remark_falls_back_to_message_fetch() -> None:
    archive = MagicMock()
    lark_client = MagicMock()
    lark_client.arequest = AsyncMock(
        return_value=_message_fetch_response(
            [
                {
                    "msg_type": "text",
                    "body": {"content": json.dumps({"text": "看这个 https://youtu.be/abc123"})},
                }
            ]
        )
    )
    pipeline = Pipeline(Settings(), lark_client, archive=archive)

    await pipeline.handle(_reply_event(root_id="om_before_restart"))

    remark = archive.enqueue.call_args.args[0]
    assert isinstance(remark, RemarkAppend)
    assert remark.url == "https://youtu.be/abc123"
    request = lark_client.arequest.call_args.args[0]
    assert request.uri == "/open-apis/im/v1/messages/om_before_restart"


async def test_reply_remark_fetch_failure_drops_with_warning(caplog) -> None:
    import logging

    archive = MagicMock()
    lark_client = MagicMock()
    lark_client.arequest = AsyncMock(return_value=_message_fetch_response([], success=False))
    pipeline = Pipeline(Settings(), lark_client, archive=archive)

    with caplog.at_level(logging.WARNING):
        await pipeline.handle(_reply_event(root_id="om_unknown"))

    archive.enqueue.assert_not_called()
    assert any("failed to fetch replied message" in r.message for r in caplog.records)


async def test_reply_to_unarchived_message_is_silently_skipped() -> None:
    archive = MagicMock()
    lark_client = MagicMock()
    lark_client.arequest = AsyncMock(
        return_value=_message_fetch_response(
            [{"msg_type": "text", "body": {"content": json.dumps({"text": "普通聊天"})}}]
        )
    )
    pipeline = Pipeline(Settings(), lark_client, archive=archive)

    await pipeline.handle(_reply_event(root_id="om_smalltalk"))

    archive.enqueue.assert_not_called()


async def test_non_reply_without_urls_is_ignored() -> None:
    archive = MagicMock()
    lark_client = MagicMock()
    lark_client.arequest = AsyncMock()
    pipeline = Pipeline(Settings(), lark_client, archive=archive)

    await pipeline.handle(_reply_event("随便聊聊"))

    archive.enqueue.assert_not_called()
    lark_client.arequest.assert_not_called()


async def test_summary_failure_log_includes_reason(caplog: pytest.LogCaptureFixture) -> None:
    pipeline = _prepare_summary_pipeline()
    pipeline._bibi_client.summarize = AsyncMock(  # type: ignore[method-assign]
        side_effect=BibiAPIError(500, '{"message":"request to bilivideo.com failed"}')
    )
    pipeline._text_sender.send = AsyncMock(return_value=True)  # type: ignore[method-assign]

    with caplog.at_level(logging.ERROR, logger="src.pipeline"):
        await pipeline._try_send_bibigpt_summary("https://youtu.be/abc123", _summary_event())

    assert "status=500" in caplog.text
    assert "reason=BibiGPT API error (HTTP 500): " in caplog.text
    assert "bilivideo.com failed" in caplog.text


def _gated_summary_pipeline(outcome: SummaryResult | Exception) -> tuple[Pipeline, asyncio.Event]:
    pipeline = _prepare_summary_pipeline()
    release = asyncio.Event()

    async def slow_summarize(url, prompt=None):
        await release.wait()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    pipeline._bibi_client.summarize = AsyncMock(side_effect=slow_summarize)  # type: ignore[method-assign]
    pipeline._sender.send = AsyncMock(return_value=True)  # type: ignore[method-assign]
    pipeline._text_sender.send = AsyncMock(return_value=True)  # type: ignore[method-assign]
    pipeline._try_send_bibigpt_chapter_summary = AsyncMock()  # type: ignore[method-assign]
    return pipeline, release


def _other_summary_event() -> CardActionEvent:
    return CardActionEvent(
        action="summarize_video",
        source_url="https://youtu.be/abc123",
        message_id="om_other",
        chat_id="oc_other_chat",
        operator_open_id="ou_user2",
    )


async def test_summary_repeat_from_same_message_is_ignored_while_in_flight() -> None:
    pipeline, release = _gated_summary_pipeline(_summary_result())
    url = "https://youtu.be/abc123"

    first = asyncio.create_task(pipeline._try_send_bibigpt_summary(url, _summary_event()))
    await asyncio.sleep(0)
    assert url in pipeline._inflight_summaries
    await pipeline._try_send_bibigpt_summary(url, _summary_event())
    assert pipeline._sender.send.await_count == 0

    release.set()
    await first

    assert pipeline._bibi_client.summarize.await_count == 1
    assert pipeline._sender.send.await_count == 1
    assert pipeline._inflight_summaries == {}


async def test_summary_concurrent_messages_share_one_generation() -> None:
    pipeline, release = _gated_summary_pipeline(_summary_result())
    url = "https://youtu.be/abc123"

    first = asyncio.create_task(pipeline._try_send_bibigpt_summary(url, _summary_event()))
    await asyncio.sleep(0)
    second = asyncio.create_task(pipeline._try_send_bibigpt_summary(url, _other_summary_event()))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)

    assert pipeline._bibi_client.summarize.await_count == 1
    chats = sorted(call.args[1] for call in pipeline._sender.send.await_args_list)
    assert chats == ["oc_chat", "oc_other_chat"]
    assert pipeline._inflight_summaries == {}


async def test_summary_shared_generation_failure_reaches_every_waiter() -> None:
    pipeline, release = _gated_summary_pipeline(BibiAPIError(500, "cdn"))
    url = "https://youtu.be/abc123"

    first = asyncio.create_task(pipeline._try_send_bibigpt_summary(url, _summary_event()))
    await asyncio.sleep(0)
    second = asyncio.create_task(pipeline._try_send_bibigpt_summary(url, _other_summary_event()))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)

    assert pipeline._bibi_client.summarize.await_count == 1
    assert pipeline._sender.send.await_count == 0
    chats = sorted(call.args[1] for call in pipeline._text_sender.send.await_args_list)
    assert chats == ["oc_chat", "oc_other_chat"]
    assert pipeline._inflight_summaries == {}


def _summary_archive_mock(content_id: str = "") -> MagicMock:
    archive = MagicMock()
    archive.find_bibigpt_content_id = AsyncMock(return_value=content_id)
    return archive


async def test_summary_success_enqueues_bibigpt_link() -> None:
    archive = _summary_archive_mock()
    pipeline = Pipeline(Settings(), MagicMock(), archive=archive)
    pipeline._typing_sender.hold = MagicMock(return_value=_NoopHold())  # type: ignore[method-assign]
    pipeline._translator.ensure_chinese_markdown_summary = AsyncMock(  # type: ignore[method-assign]
        return_value="- **总结**\n    - 内容"
    )
    pipeline._bibi_client.summarize = AsyncMock(return_value=_summary_result())  # type: ignore[method-assign]
    pipeline._sender.send = AsyncMock(return_value=True)  # type: ignore[method-assign]
    pipeline._try_send_bibigpt_chapter_summary = AsyncMock()  # type: ignore[method-assign]

    await pipeline._try_send_bibigpt_summary("https://youtu.be/abc123", _summary_event())

    update = archive.enqueue.call_args.args[0]
    assert isinstance(update, BibiLinkUpdate)
    assert update.url == "https://youtu.be/abc123"
    # The site's share page, built from the summary's contentId at the origin
    # of the default bibigpt_base_url (https://aitodo.co/zh).
    assert update.bibigpt_url == "https://aitodo.co/content/content-123"
    # The same link is shown inside the summary card itself.
    card_json = pipeline._sender.send.call_args.args[0]
    assert "[BibiGPT 页面](https://aitodo.co/content/content-123)" in card_json


async def test_summary_success_maps_source_url_to_archived_url() -> None:
    archive = _summary_archive_mock()
    pipeline = _prepare_card_pipeline(archive)
    await pipeline._process_url("https://youtu.be/abc123", _link_message_event(), False, False)
    pipeline._bibi_client.summarize = AsyncMock(return_value=_summary_result())  # type: ignore[method-assign]
    pipeline._translator.ensure_chinese_markdown_summary = AsyncMock(  # type: ignore[method-assign]
        return_value="- **总结**\n    - 内容"
    )
    pipeline._try_send_bibigpt_chapter_summary = AsyncMock()  # type: ignore[method-assign]

    await pipeline._try_send_bibigpt_summary("https://youtu.be/abc123", _summary_event())

    update = archive.enqueue.call_args.args[0]
    assert isinstance(update, BibiLinkUpdate)
    assert update.url == "https://www.youtube.com/watch?v=abc123"
    assert update.bibigpt_url == "https://aitodo.co/content/content-123"


async def test_summary_card_send_failure_does_not_enqueue_bibigpt_link() -> None:
    archive = _summary_archive_mock()
    pipeline = Pipeline(Settings(), MagicMock(), archive=archive)
    pipeline._typing_sender.hold = MagicMock(return_value=_NoopHold())  # type: ignore[method-assign]
    pipeline._translator.ensure_chinese_markdown_summary = AsyncMock(  # type: ignore[method-assign]
        return_value="- **总结**\n    - 内容"
    )
    pipeline._bibi_client.summarize = AsyncMock(return_value=_summary_result())  # type: ignore[method-assign]
    pipeline._sender.send = AsyncMock(return_value=False)  # type: ignore[method-assign]
    pipeline._text_sender.send = AsyncMock()  # type: ignore[method-assign]

    await pipeline._try_send_bibigpt_summary("https://youtu.be/abc123", _summary_event())

    archive.enqueue.assert_not_called()


def _cached_summary_pipeline(archive) -> Pipeline:
    pipeline = Pipeline(Settings(), MagicMock(), archive=archive)
    pipeline._typing_sender.hold = MagicMock(return_value=_NoopHold())  # type: ignore[method-assign]
    pipeline._translator.ensure_chinese_markdown_summary = AsyncMock(  # type: ignore[method-assign]
        return_value="- **总结**\n    - 内容"
    )
    pipeline._sender.send = AsyncMock(return_value=True)  # type: ignore[method-assign]
    pipeline._try_send_bibigpt_chapter_summary = AsyncMock()  # type: ignore[method-assign]
    return pipeline


async def test_repeat_summary_uses_cached_lookup_instead_of_regenerating() -> None:
    pipeline = _cached_summary_pipeline(_summary_archive_mock())
    pipeline._bibi_client.summarize = AsyncMock(return_value=_summary_result())  # type: ignore[method-assign]
    pipeline._bibi_client.summarize_cached = AsyncMock(return_value=_summary_result())  # type: ignore[method-assign]

    await pipeline._try_send_bibigpt_summary("https://youtu.be/abc123", _summary_event())
    pipeline._bibi_client.summarize.assert_awaited_once()

    await pipeline._try_send_bibigpt_summary("https://youtu.be/abc123", _summary_event())

    pipeline._bibi_client.summarize.assert_awaited_once()
    pipeline._bibi_client.summarize_cached.assert_awaited_once()


async def test_summary_cache_marker_survives_via_archive_column() -> None:
    pipeline = _cached_summary_pipeline(_summary_archive_mock(content_id="content-999"))
    pipeline._bibi_client.summarize = AsyncMock(return_value=_summary_result())  # type: ignore[method-assign]
    pipeline._bibi_client.summarize_cached = AsyncMock(return_value=_summary_result())  # type: ignore[method-assign]

    await pipeline._try_send_bibigpt_summary("https://youtu.be/abc123", _summary_event())

    pipeline._bibi_client.summarize_cached.assert_awaited_once()
    pipeline._bibi_client.summarize.assert_not_called()


async def test_summary_cache_lookup_failure_falls_back_to_regeneration() -> None:
    pipeline = _cached_summary_pipeline(_summary_archive_mock(content_id="content-999"))
    pipeline._bibi_client.summarize = AsyncMock(return_value=_summary_result())  # type: ignore[method-assign]
    pipeline._bibi_client.summarize_cached = AsyncMock(return_value=None)  # type: ignore[method-assign]

    await pipeline._try_send_bibigpt_summary("https://youtu.be/abc123", _summary_event())

    pipeline._bibi_client.summarize_cached.assert_awaited_once()
    pipeline._bibi_client.summarize.assert_awaited_once()


async def test_summary_with_custom_prompt_skips_cache() -> None:
    pipeline = _cached_summary_pipeline(_summary_archive_mock(content_id="content-999"))
    pipeline._bibi_client.summarize = AsyncMock(return_value=_summary_result())  # type: ignore[method-assign]
    pipeline._bibi_client.summarize_cached = AsyncMock(return_value=_summary_result())  # type: ignore[method-assign]

    event = MessageEvent(
        message_id="msg_prompt",
        chat_id="oc_chat",
        sender_id="ou_sender",
        chat_type="group",
        timestamp_utc=0,
        message_type="text",
        content=json.dumps({"text": "@_user_1 重点讲马编经历 https://youtu.be/abc123"}),
        mentions=[],
    )
    await pipeline._try_send_bibigpt_summary("https://youtu.be/abc123", event)

    pipeline._bibi_client.summarize_cached.assert_not_called()
    pipeline._bibi_client.summarize.assert_awaited_once()
