from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bibi_client import AuthenticationError, BibiAPIError, TranscriptUnavailableError
from src.comment_analyzer import CommentAnalysisError
from src.config import Settings
from src.pipeline import (
    Pipeline,
    _comment_analysis_failure_message,
    _friendly_download_reason,
    _summary_failure_message,
)


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
