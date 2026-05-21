import httpx

from src.config import Settings
from src.dispatch import (
    Dispatcher,
    _fallback_media_type,
    _fallback_title,
    _friendly_parse_warning,
    _normalize_instagram_post_meta,
    _normalize_x_post_meta,
)
from src.parsers.base import LinkMetadata, MediaType, ParserError


class _StaticParser:
    def __init__(self, meta: LinkMetadata) -> None:
        self.meta = meta
        self.called = False

    async def parse(self, url: str) -> LinkMetadata:
        self.called = True
        return self.meta


class _FailIfCalledParser:
    async def parse(self, url: str) -> LinkMetadata:
        raise AssertionError(f"unexpected parser call for {url}")


class _ParserErrorParser:
    async def parse(self, url: str) -> LinkMetadata:
        raise ParserError(url, "intentional test failure")


async def test_instagram_post_uses_media_info_not_ytdlp() -> None:
    async with httpx.AsyncClient() as client:
        dispatcher = Dispatcher(Settings(), client)
        media_info = _StaticParser(
            LinkMetadata(
                source_url="https://www.instagram.com/p/DYkarnvCH-O/",
                title="Post by al.yasid",
                description="Ferrari F40 carousel",
                cover_url="https://cdn.example.com/f40.jpg",
                platform="instagram",
                media_type=MediaType.ARTICLE,
            )
        )
        dispatcher._instagram_media_info = media_info  # type: ignore[assignment]
        dispatcher._ytdlp = _FailIfCalledParser()  # type: ignore[assignment]
        dispatcher._og = _ParserErrorParser()  # type: ignore[assignment]

        meta = await dispatcher.parse("https://www.instagram.com/p/DYkarnvCH-O/")

    assert media_info.called is True
    assert meta.cover_url == "https://cdn.example.com/f40.jpg"
    assert meta.media_type == MediaType.ARTICLE


async def test_instagram_reel_still_uses_ytdlp() -> None:
    async with httpx.AsyncClient() as client:
        dispatcher = Dispatcher(Settings(), client)
        ytdlp = _StaticParser(
            LinkMetadata(
                source_url="https://www.instagram.com/reel/DWgZM2wEz1A/",
                title="Video by creator",
                cover_url="https://cdn.example.com/reel.jpg",
                platform="instagram",
                media_type=MediaType.VIDEO,
            )
        )
        dispatcher._ytdlp = ytdlp  # type: ignore[assignment]
        dispatcher._instagram_media_info = _FailIfCalledParser()  # type: ignore[assignment]

        meta = await dispatcher.parse("https://www.instagram.com/reel/DWgZM2wEz1A/")

    assert ytdlp.called is True
    assert meta.media_type == MediaType.VIDEO


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


def test_tiktok_post_without_video_is_treated_as_image_post() -> None:
    url = "https://www.tiktok.com/@example/photo/123"
    reason = "yt-dlp metadata failed: ERROR: [TikTok] 123: No video formats found!"

    assert _fallback_media_type(url, "tiktok", reason) == MediaType.ARTICLE
    assert _fallback_title(url, "tiktok", MediaType.ARTICLE) == "TikTok Post"
    assert _friendly_parse_warning(url, "tiktok", reason) == ""


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


def test_normalize_x_post_drops_placeholder_page() -> None:
    meta = LinkMetadata(
        source_url="https://x.com/example/status/123",
        title="x.com",
        description="x.com",
        platform="x",
        media_type=MediaType.ARTICLE,
    )

    _normalize_x_post_meta(meta)

    assert meta.title == "X Post"
    assert meta.description == ""
    assert meta.parse_warnings == [
        "X 内容受限或需要 cookie, 无法获取正文, 已先发送卡片"
    ]
