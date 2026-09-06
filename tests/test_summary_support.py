import pytest

from src.parsers.base import LinkMetadata, MediaType
from src.summary_support import (
    canonical_summary_url,
    is_summary_video_url,
    summary_url_for_metadata,
    supports_video_summary,
)


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        (
            "https://www.tiktok.com/@genna.bug/video/7678926593120587038?is_from_webapp=1",
            "https://www.tiktok.com/@genna.bug/video/7678926593120587038",
        ),
        (
            "https://m.tiktok.com/@creator/video/123/#comments",
            "https://www.tiktok.com/@creator/video/123",
        ),
        ("https://www.douyin.com/video/123?from=share", "https://www.douyin.com/video/123"),
        (
            "https://www.iesdouyin.com/share/video/123/?region=CN",
            "https://www.douyin.com/video/123",
        ),
        ("https://www.douyin.com/?modal_id=123", "https://www.douyin.com/video/123"),
        (
            "https://www.douyin.com/user/MS4w?modal_id=123&from=share",
            "https://www.douyin.com/video/123",
        ),
        (
            "https://www.douyin.com/note/123?modal_id=456",
            "https://www.douyin.com/note/123",
        ),
        ("https://www.douyin.com/slides/123?modal_id=456", None),
        (
            "https://www.tiktok.com/@creator/photo/123?is_from_webapp=1",
            "https://www.tiktok.com/@creator/photo/123",
        ),
        (
            "https://www.bilibili.com/video/BV1BCGB66E8P/?p=2&vd_source=shared",
            "https://www.bilibili.com/video/BV1BCGB66E8P?p=2",
        ),
        ("http://m.bilibili.com/video/av123?p=3", "https://www.bilibili.com/video/av123?p=3"),
        ("https://youtu.be/RMzaJHBSPCw?si=shared", "https://www.youtube.com/watch?v=RMzaJHBSPCw"),
        (
            "https://www.youtube.com/shorts/RMzaJHBSPCw?feature=share",
            "https://www.youtube.com/watch?v=RMzaJHBSPCw",
        ),
        (
            "https://www.youtube.com/watch?v=RMzaJHBSPCw&si=shared",
            "https://www.youtube.com/watch?v=RMzaJHBSPCw",
        ),
        ("https://vm.tiktok.com/short/", "https://vm.tiktok.com/short"),
        ("https://v.douyin.com/short/", "https://v.douyin.com/short"),
        ("https://youtu.be/abc123", "https://youtu.be/abc123"),
        ("https://example.com/?next=www.douyin.com/video/123", None),
        ("not a URL", None),
        ("https://[bad", None),
    ],
)
def test_summary_urls_normalize_content_without_network(
    original: str, expected: str | None,
) -> None:
    result = canonical_summary_url(original)
    assert result == (original if expected is None else expected)
    assert canonical_summary_url(result) == result


@pytest.mark.parametrize(
    "url",
    [
        "https://www.tiktok.com/@creator/video/123",
        "https://www.douyin.com/video/123",
        "https://www.iesdouyin.com/share/video/123/",
        "https://www.douyin.com/?modal_id=123",
        "https://www.bilibili.com/video/BV1BCGB66E8P/?p=2",
        "https://youtu.be/RMzaJHBSPCw",
    ],
)
def test_summary_video_urls_include_only_known_video_targets(url: str) -> None:
    assert is_summary_video_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.tiktok.com/@creator/photo/123",
        "https://www.douyin.com/note/123?modal_id=456",
        "https://www.iesdouyin.com/share/note/123/",
        "https://www.douyin.com/note/invalid?modal_id=456",
        "https://www.douyin.com/photo/123?modal_id=456",
        "https://www.douyin.com/slides/123?modal_id=456",
        "https://www.iesdouyin.com/share/slides/123?modal_id=456",
        "https://vm.tiktok.com/short/",
        "https://v.douyin.com/short/",
        "https://www.tiktok.com/@creator",
        "https://www.douyin.com/user/creator",
        "https://www.douyin.com/video/invalid",
        "https://tiktok.com.evil.example/@creator/video/123",
        "https://evil.example/douyin.com/video/123",
        "https://youtube.com.evil.example/watch?v=RMzaJHBSPCw",
        "https://bilibili.com@evil.example/video/BV1BCGB66E8P",
        "javascript://www.douyin.com/video/123",
        "https://[bad",
    ],
)
def test_summary_video_urls_reject_photos_unresolved_and_lookalikes(url: str) -> None:
    assert not is_summary_video_url(url)


@pytest.mark.parametrize(
    ("platform", "url"),
    [
        ("youtube", "https://youtu.be/abc123"),
        ("bilibili", "https://www.bilibili.com/video/BV1short"),
        ("tiktok", "https://www.tiktok.com/@creator/video/123"),
        ("douyin", "https://www.douyin.com/video/123"),
    ],
)
@pytest.mark.parametrize("content", ["title", "caption", "verified_cover"])
def test_supported_real_videos_allow_summary_without_download_metadata(
    platform: str, url: str, content: str,
) -> None:
    meta = LinkMetadata(source_url=url, platform=platform, media_type=MediaType.VIDEO)
    if content == "title":
        meta.title = "A real video title"
    elif content == "caption":
        meta.description = "The garden after an afternoon of rain."
    else:
        meta.content_verified = True
        meta.cover_url = "https://cdn.example/real-cover.jpg"
    assert supports_video_summary(meta)
    assert not meta.download_candidates


@pytest.mark.parametrize("media_type", [MediaType.ARTICLE, MediaType.UNKNOWN])
@pytest.mark.parametrize("platform", ["tiktok", "douyin"])
def test_social_posts_need_a_confirmed_video_type(platform: str, media_type: MediaType) -> None:
    meta = LinkMetadata(
        source_url=f"https://www.{platform}.com/video/123", platform=platform,
        title="Real caption", media_type=media_type,
    )
    assert not supports_video_summary(meta)


@pytest.mark.parametrize(
    "title", ["", "douyin.com", "抖音 - 记录美好生活", "内容暂未获取", "Log in to Douyin"],
)
def test_placeholders_and_unverified_cover_do_not_enable_summary(title: str) -> None:
    meta = LinkMetadata(
        source_url="https://www.douyin.com/video/123", platform="douyin",
        title=title, media_type=MediaType.VIDEO, cover_url="https://cdn.example/logo.jpg",
    )
    assert not supports_video_summary(meta)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.tiktok.com/@creator/photo/123",
        "https://www.douyin.com/note/123?modal_id=456",
    ],
)
def test_photo_urls_do_not_enable_summary_even_if_metadata_mislabels_video(url: str) -> None:
    meta = LinkMetadata(source_url=url, title="Real post caption", media_type=MediaType.VIDEO)
    assert not supports_video_summary(meta)


def test_source_share_link_uses_validated_canonical_target() -> None:
    meta = LinkMetadata(
        source_url="https://v.douyin.com/share/",
        canonical_url="https://www.douyin.com/video/123", platform="douyin",
        description="Real caption", media_type=MediaType.VIDEO,
    )
    assert supports_video_summary(meta)
    meta.canonical_url = "https://www.douyin.com/note/123"
    assert not supports_video_summary(meta)


def test_platform_label_cannot_turn_an_unrelated_host_into_a_summary_source() -> None:
    meta = LinkMetadata(
        source_url="https://example.com/douyin.com/video/123", platform="douyin",
        title="Real title", media_type=MediaType.VIDEO,
    )
    assert not supports_video_summary(meta)


def test_missing_platform_label_can_use_the_real_url_domain() -> None:
    meta = LinkMetadata(
        source_url="https://youtu.be/abc123", title="Real title", media_type=MediaType.VIDEO,
    )
    assert supports_video_summary(meta)


@pytest.mark.parametrize(
    "source",
    [
        "https://www.bilibili.com/video/BV1BCGB66E8P/?p=2&vd_source=share",
        "https://m.bilibili.com/video/BV1BCGB66E8P?p=02",
        "https://b23.tv/shared?p=2",
    ],
)
def test_metadata_summary_preserves_the_shared_part_missing_from_canonical(source: str) -> None:
    canonical = "https://www.bilibili.com/video/BV1BCGB66E8P/"
    second_part = summary_url_for_metadata(LinkMetadata(
        source_url=source, canonical_url=canonical,
    ))
    first_part = summary_url_for_metadata(LinkMetadata(
        source_url="https://www.bilibili.com/video/BV1BCGB66E8P?p=1", canonical_url=canonical,
    ))
    assert second_part == canonical.rstrip("/") + "?p=2"
    assert first_part == canonical.rstrip("/")
    assert second_part != first_part


def test_metadata_summary_preserves_av_id_part() -> None:
    assert summary_url_for_metadata(LinkMetadata(
        source_url="https://www.bilibili.com/video/av123?p=3",
        canonical_url="https://www.bilibili.com/video/av123",
    )) == "https://www.bilibili.com/video/av123?p=3"


@pytest.mark.parametrize(
    "source",
    [
        "https://www.bilibili.com/video/BV1BCGB66E8X?p=2",
        "https://www.bilibili.com/video/av123?p=2",
        "https://www.bilibili.com/read/cv123?p=2",
        "https://evil.example/b23.tv/shared?p=2",
        "https://b23.tv.evil.example/shared?p=2",
        "https://b23.tv/video/BV1BCGB66E8X?p=2",
    ],
)
def test_metadata_summary_does_not_transfer_parts_from_unrelated_content(source: str) -> None:
    canonical = "https://www.bilibili.com/video/BV1BCGB66E8P"
    assert summary_url_for_metadata(LinkMetadata(
        source_url=source, canonical_url=canonical,
    )) == canonical


@pytest.mark.parametrize("page", ["", "1", "0", "-2", "NaN"])
def test_metadata_summary_keeps_existing_canonical_part_when_source_has_no_later_part(
    page: str,
) -> None:
    canonical = "https://www.bilibili.com/video/BV1BCGB66E8P?p=3"
    assert summary_url_for_metadata(LinkMetadata(
        source_url=f"https://www.bilibili.com/video/BV1BCGB66E8P?p={page}",
        canonical_url=canonical,
    )) == canonical
