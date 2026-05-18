from feishu_link.dispatch import (
    _fallback_media_type,
    _fallback_title,
    _friendly_parse_warning,
    _normalize_instagram_post_meta,
    _normalize_x_post_meta,
)
from feishu_link.parsers.base import LinkMetadata, MediaType


def test_instagram_post_without_video_is_treated_as_image_post() -> None:
    url = "https://www.instagram.com/p/DXPd2NUiM2n/?img_index=3"
    reason = "yt-dlp metadata failed: ERROR: [Instagram] DXPd2NUiM2n: No video formats found!"

    assert _fallback_media_type(url, "instagram", reason) == MediaType.ARTICLE
    assert _fallback_title(url, "instagram", MediaType.ARTICLE) == "Instagram Post"
    assert _friendly_parse_warning(url, "instagram", reason) == ""


def test_instagram_reel_parse_failure_still_reports_video_failure() -> None:
    url = "https://www.instagram.com/reel/DYFxpjpN-ao/"
    reason = "yt-dlp metadata failed: transient extractor failure"

    assert _fallback_media_type(url, "instagram", reason) == MediaType.VIDEO
    assert _fallback_title(url, "instagram", MediaType.VIDEO) == "Instagram Reel"
    assert (
        _friendly_parse_warning(url, "instagram", reason)
        == "instagram 视频解析失败, 已先发送卡片"
    )


def test_x_post_without_video_is_treated_as_image_post() -> None:
    url = "https://x.com/example/status/123"
    reason = "yt-dlp metadata failed: ERROR: [twitter] 123: No video formats found!"

    assert _fallback_media_type(url, "x", reason) == MediaType.ARTICLE
    assert _fallback_title(url, "x", MediaType.ARTICLE) == "X Post"
    assert _friendly_parse_warning(url, "x", reason) == ""


def test_normalize_instagram_post_extracts_caption_author_and_counts() -> None:
    meta = LinkMetadata(
        source_url="https://www.instagram.com/p/DXPd2NUiM2n/?img_index=3",
        title=(
            'Rebecca Kunnis on Instagram: "BMW M1 widebody, '
            'covered in custom rhinestone artwork"'
        ),
        description=(
            '77K likes, 56 comments - beccu.studio on April 17, 2026: '
            '"BMW M1 widebody, covered in custom rhinestone artwork. '
            'Would u drive it? Made w @krea_ai <3".'
        ),
        platform="instagram",
        media_type=MediaType.ARTICLE,
    )

    _normalize_instagram_post_meta(meta)

    assert meta.title == "Post by beccu.studio"
    assert meta.channel == "beccu.studio"
    assert meta.description == (
        "BMW M1 widebody, covered in custom rhinestone artwork. "
        "Would u drive it? Made w @krea_ai <3"
    )
    assert meta.like_count == 77000
    assert meta.comment_count == 56


def test_normalize_x_post_extracts_text_and_author() -> None:
    meta = LinkMetadata(
        source_url="https://x.com/example/status/123",
        title="Example on X: \"A compact electric wagon concept with solar roof\" / X",
        description="",
        channel="example",
        platform="x",
        media_type=MediaType.ARTICLE,
        like_count=123,
        repost_count=9,
    )

    _normalize_x_post_meta(meta)

    assert meta.title == "Post by @example"
    assert meta.channel == "@example"
    assert meta.description == "A compact electric wagon concept with solar roof"
    assert meta.like_count == 123
    assert meta.repost_count == 9
