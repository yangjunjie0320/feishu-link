from feishu_link.dispatch import (
    _fallback_media_type,
    _fallback_title,
    _friendly_parse_warning,
)
from feishu_link.parsers.base import MediaType


def test_instagram_post_without_video_is_treated_as_image_post() -> None:
    url = "https://www.instagram.com/p/DXPd2NUiM2n/?img_index=3"
    reason = "yt-dlp metadata failed: ERROR: [Instagram] DXPd2NUiM2n: No video formats found!"

    assert _fallback_media_type(url, "instagram", reason) == MediaType.ARTICLE
    assert _fallback_title(url, "instagram", MediaType.ARTICLE) == "Instagram Post"
    assert (
        _friendly_parse_warning(url, "instagram", reason)
        == "instagram 图文内容已发送卡片, 未发现可下载视频"
    )


def test_instagram_reel_parse_failure_still_reports_video_failure() -> None:
    url = "https://www.instagram.com/reel/DYFxpjpN-ao/"
    reason = "yt-dlp metadata failed: transient extractor failure"

    assert _fallback_media_type(url, "instagram", reason) == MediaType.VIDEO
    assert _fallback_title(url, "instagram", MediaType.VIDEO) == "Instagram Reel"
    assert (
        _friendly_parse_warning(url, "instagram", reason)
        == "instagram 视频解析失败, 已先发送卡片"
    )
