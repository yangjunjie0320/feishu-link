from src.bibi_client import AuthenticationError, BibiAPIError
from src.pipeline import _friendly_download_reason, _summary_failure_message


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
        BibiAPIError(500, "<!DOCTYPE html><html><head></head></html>")
    )

    assert message == (
        "BibiGPT 总结失败: 服务返回了 HTML 错误页 (HTTP 500), "
        "通常是登录态异常或 BibiGPT 服务端临时异常。"
    )
