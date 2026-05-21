from types import SimpleNamespace

import pytest

from src.config import Settings
from src.parsers.base import MediaType
from src.parsers.ytdlp import YtDlpMetadataParser, _metadata_from_info


def test_metadata_from_ytdlp_info() -> None:
    meta = _metadata_from_info(
        "https://www.tiktok.com/@u/video/123",
        "tiktok",
        {
            "title": "Short clip",
            "description": "hello",
            "thumbnail": "https://example.com/cover.jpg",
            "duration": 12.3,
            "view_count": 123456,
            "like_count": 7890,
            "comment_count": 123,
            "repost_count": 45,
            "uploader": "creator",
            "webpage_url": "https://www.tiktok.com/@u/video/123",
            "formats": [{
                "url": "https://cdn.example.com/video.mp4?token=secret",
                "format_id": "18",
                "ext": "mp4",
                "filesize": 1234,
            }],
        },
    )

    assert meta.platform == "tiktok"
    assert meta.site_name == "TikTok"
    assert meta.media_type == MediaType.VIDEO
    assert meta.duration_seconds == 12
    assert meta.channel == "creator"
    assert meta.view_count == 123456
    assert meta.like_count == 7890
    assert meta.comment_count == 123
    assert meta.repost_count == 45
    assert meta.download_candidates[0].format_id == "18"


def test_metadata_uses_nested_thumbnail_for_image_posts() -> None:
    meta = _metadata_from_info(
        "https://www.instagram.com/p/abc/",
        "instagram",
        {
            "title": "Instagram Post",
            "description": "caption",
            "entries": [{
                "id": "image-1",
                "thumbnails": [{
                    "url": "https://cdn.example.com/photo.webp?token=secret",
                }],
            }],
        },
    )

    assert meta.cover_url == "https://cdn.example.com/photo.webp?token=secret"


@pytest.mark.asyncio
async def test_ytdlp_parser_uses_unified_cookie_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    captured_options = {}

    class FakeYoutubeDL:
        def __init__(self, options):
            captured_options.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            return {
                "title": "Video",
                "duration": 12,
                "webpage_url": url,
                "formats": [{"url": "https://cdn.example.com/v.mp4"}],
            }

    monkeypatch.setitem(
        __import__("sys").modules,
        "yt_dlp",
        SimpleNamespace(YoutubeDL=FakeYoutubeDL),
    )

    parser = YtDlpMetadataParser(Settings(cookie_file=str(cookie_file)))
    meta = await parser.parse("https://www.bilibili.com/video/BV123")

    assert meta.title == "Video"
    assert captured_options["cookiefile"].endswith("cookies.txt")
