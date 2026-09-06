import asyncio

import pytest

from src.card_metadata import CardMetadataCache, card_result, content_key, merge_metadata
from src.parsers.base import CardParseResult, CardStatus, LinkMetadata, MediaType

URL = "https://x.com/author/status/123"


@pytest.mark.parametrize("title", [
    "x.com", "Post by @author", "Log in / X", "Just a moment...",
    "From breaking news and entertainment to sports and politics",
])
def test_placeholders_do_not_become_content(title: str) -> None:
    result = card_result(LinkMetadata(source_url=URL, title=title, platform="x"))
    assert result.status == CardStatus.UNAVAILABLE
    assert result.has_content is False
    assert result.metadata.title == ""


def test_text_only_is_complete_only_when_media_absence_is_confirmed() -> None:
    meta = LinkMetadata(source_url=URL, description="The real post", platform="x")
    assert card_result(meta).status == CardStatus.PARTIAL
    meta.has_visual = False
    assert card_result(meta).status == CardStatus.COMPLETE


def test_verified_captionless_image_is_complete_but_a_site_logo_is_not() -> None:
    meta = LinkMetadata(source_url=URL, cover_url="https://cdn.example/image.jpg", platform="x")
    assert card_result(meta).status == CardStatus.UNAVAILABLE
    meta.content_verified = True
    assert card_result(meta).status == CardStatus.COMPLETE


def test_video_without_cover_keeps_real_title_as_partial() -> None:
    meta = LinkMetadata(source_url=URL, title="Real video", media_type=MediaType.VIDEO)
    result = card_result(meta)
    assert result.status == CardStatus.PARTIAL
    assert result.has_content


@pytest.mark.parametrize("media_type", [MediaType.VIDEO, MediaType.UNKNOWN])
def test_cover_without_caption_does_not_complete_video_or_unknown_content(
    media_type: MediaType,
) -> None:
    result = card_result(LinkMetadata(
        source_url="https://www.tiktok.com/@creator/video/123", platform="tiktok",
        media_type=media_type, cover_url="https://cdn.example/verified-cover.jpg",
        has_visual=True, content_verified=True,
    ))
    assert result.status == CardStatus.PARTIAL
    assert result.has_content is True


def test_real_article_preview_replaces_only_a_link_without_overwriting_real_caption() -> None:
    meta = LinkMetadata(source_url=URL, description="https://t.co/example", platform="x")
    article = LinkMetadata(source_url=URL, description="The article preview", platform="x")
    merge_metadata(meta, article)
    assert meta.description == "The article preview"
    merge_metadata(meta, LinkMetadata(source_url=URL, description="Another preview", platform="x"))
    assert meta.description == "The article preview"


def test_merge_replaces_placeholder_preserves_real_fields_and_signed_cover_candidates() -> None:
    target = LinkMetadata(source_url=URL, title="x.com", description="Original post", platform="x")
    source = LinkMetadata(
        source_url=URL, title="Real title", description="Other description", platform="x",
        cover_url="//cdn.example/a.jpg?sig=first&expires=42",
        cover_candidates=["https://cdn.example/b.jpg", "https://cdn.example/c.jpg",
                          "https://cdn.example/d.jpg"],
        like_count=0, content_verified=True,
    )
    merge_metadata(target, source)
    assert target.title == "Real title"
    assert target.description == "Original post"
    assert target.like_count == 0
    assert target.cover_candidates == [
        "https://cdn.example/a.jpg?sig=first&expires=42",
        "https://cdn.example/b.jpg", "https://cdn.example/c.jpg",
    ]
    assert source.cover_url.startswith("//")


@pytest.mark.parametrize(("platform", "url"), [
    ("instagram", "https://www.instagram.com/reel/PhotoPost/"),
    ("douyin", "https://www.douyin.com/video/1234567890"),
])
def test_verified_photo_content_overrides_video_url_guess(platform: str, url: str) -> None:
    target = LinkMetadata(source_url=url, platform=platform, media_type=MediaType.VIDEO)
    source = LinkMetadata(
        source_url=url, platform=platform, media_type=MediaType.ARTICLE,
        cover_url="https://cdn.example/photo.jpg", has_visual=True, content_verified=True,
    )

    merge_metadata(target, source)

    assert target.media_type == MediaType.ARTICLE
    assert target.content_verified is True
    assert card_result(target).status == CardStatus.COMPLETE


@pytest.mark.parametrize(("platform", "url"), [
    ("bilibili", "https://www.bilibili.com/video/BV1J5ta6cEYp/"),
    ("youtube", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
])
def test_video_platform_type_survives_verified_generic_article_metadata(
    platform: str, url: str,
) -> None:
    target = LinkMetadata(source_url=url, platform=platform, media_type=MediaType.VIDEO)

    merge_metadata(target, LinkMetadata(
        source_url=url, platform=platform, media_type=MediaType.ARTICLE,
        title="A verified page title", content_verified=True,
    ))

    assert target.media_type == MediaType.VIDEO


@pytest.mark.parametrize(("target_verified", "source_verified", "source_type"), [
    (False, False, MediaType.ARTICLE),
    (True, True, MediaType.ARTICLE),
    (False, True, MediaType.UNKNOWN),
])
def test_social_type_correction_requires_new_verified_known_content(
    target_verified: bool, source_verified: bool, source_type: MediaType,
) -> None:
    target = LinkMetadata(
        source_url=URL, platform="x", media_type=MediaType.VIDEO,
        content_verified=target_verified,
    )

    merge_metadata(target, LinkMetadata(
        source_url=URL, platform="x", media_type=source_type,
        content_verified=source_verified,
    ))

    assert target.media_type == MediaType.VIDEO


@pytest.mark.parametrize(("left", "right"), [
    ("https://youtu.be/dQw4w9WgXcQ?si=tracking",
     "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ("https://twitter.com/author/status/123/video/1?s=20", URL),
    ("https://www.instagram.com/p/abc/?img_index=1", "https://instagram.com/reel/abc/"),
    ("https://www.tiktok.com/@old/video/123", "https://www.tiktok.com/@new/video/123"),
])
def test_content_keys_share_supported_aliases(left: str, right: str) -> None:
    assert content_key(left) == content_key(right)


def test_instagram_selected_image_is_not_merged_with_another_image() -> None:
    assert content_key("https://instagram.com/p/abc/?img_index=2") != content_key(
        "https://instagram.com/p/abc/?img_index=3"
    )


def _complete(url: str = URL) -> CardParseResult:
    return card_result(LinkMetadata(source_url=url, description="Actual content", has_visual=False))


async def test_shared_work_survives_waiter_cancellation_and_returns_independent_copies() -> None:
    cache = CardMetadataCache()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def fetch() -> CardParseResult:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return _complete()

    first = asyncio.create_task(cache.get(URL, fetch))
    await entered.wait()
    alias = "https://twitter.com/another/status/123/video/1"
    second = asyncio.create_task(cache.get(alias, fetch))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    result = await second
    assert calls == 1
    assert result.metadata.source_url == alias
    result.metadata.description = "Changed by translator"
    result.sources.append("changed")
    cached = await cache.get(URL, fetch)
    assert cached.metadata.description == "Actual content"
    assert cached.sources == []
    assert cached.metadata.source_url == URL


async def test_cache_expiry_lru_and_explicit_invalidation(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr("src.card_metadata.time.monotonic", lambda: now[0])
    cache = CardMetadataCache(ttl=10, capacity=2)
    calls = 0

    async def fetch() -> CardParseResult:
        nonlocal calls
        calls += 1
        return _complete()

    second = "https://x.com/a/status/456"
    third = "https://x.com/a/status/789"
    await cache.get(URL, fetch)
    await cache.get(second, fetch)
    await cache.get(URL, fetch)
    await cache.get(third, fetch)
    await cache.get(second, fetch)
    assert calls == 4
    cache.invalidate(second)
    await cache.get(second, fetch)
    assert calls == 5
    now[0] += 11
    await cache.get(second, fetch)
    assert calls == 6


async def test_partial_result_is_not_cached_as_success() -> None:
    cache = CardMetadataCache()
    calls = 0

    async def fetch() -> CardParseResult:
        nonlocal calls
        calls += 1
        return card_result(LinkMetadata(source_url=URL, description="Actual content"))

    assert (await cache.get(URL, fetch)).status == CardStatus.PARTIAL
    await cache.get(URL, fetch)
    assert calls == 2
