import httpx
import pytest
import respx

from src.parsers.youtube import YouTubeParser, extract_video_id, is_youtube_url

YOUTUBE_API_RESPONSE = {
    "items": [{
        "snippet": {
            "title": "Rick Roll",
            "description": "Never gonna give you up",
            "channelTitle": "Rick Astley",
            "thumbnails": {
                "high": {"url": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"}
            },
        },
        "contentDetails": {"duration": "PT3M33S"},
        "statistics": {
            "viewCount": "1000000",
            "likeCount": "12000",
            "commentCount": "345",
        },
    }]
}

OG_YOUTUBE_HTML = """<!DOCTYPE html>
<html><head>
  <meta property="og:title" content="Rick Roll" />
  <meta property="og:image" content="https://i.ytimg.com/img.jpg" />
</head><body></body></html>"""


@pytest.mark.parametrize("url,expected", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://example.com", None),
])
def test_extract_video_id(url: str, expected: str | None) -> None:
    assert extract_video_id(url) == expected


def test_is_youtube_url() -> None:
    assert is_youtube_url("https://youtu.be/abc123defgh") is True
    assert is_youtube_url("https://example.com") is False


@respx.mock
async def test_youtube_api_success() -> None:
    respx.get("https://www.googleapis.com/youtube/v3/videos").mock(
        return_value=httpx.Response(200, json=YOUTUBE_API_RESPONSE)
    )
    async with httpx.AsyncClient() as client:
        parser = YouTubeParser(client, api_key="fake_key")
        meta = await parser.parse("https://youtu.be/dQw4w9WgXcQ")

    assert meta.title == "Rick Roll"
    assert meta.channel == "Rick Astley"
    assert meta.duration_seconds == 213  # 3m33s
    assert meta.site_name == "YouTube"
    assert meta.view_count == 1000000
    assert meta.like_count == 12000
    assert meta.comment_count == 345


@respx.mock
async def test_youtube_api_fallback_to_og() -> None:
    respx.get("https://www.googleapis.com/youtube/v3/videos").mock(
        return_value=httpx.Response(403)
    )
    respx.get("https://www.youtube.com/watch?v=dQw4w9WgXcQ").mock(
        return_value=httpx.Response(200, text=OG_YOUTUBE_HTML)
    )
    async with httpx.AsyncClient() as client:
        parser = YouTubeParser(client, api_key="fake_key")
        meta = await parser.parse("https://youtu.be/dQw4w9WgXcQ")

    assert meta.title == "Rick Roll"
    assert meta.duration_seconds is None


@respx.mock
async def test_youtube_no_api_key_uses_og() -> None:
    respx.get("https://www.youtube.com/watch?v=dQw4w9WgXcQ").mock(
        return_value=httpx.Response(200, text=OG_YOUTUBE_HTML)
    )
    async with httpx.AsyncClient() as client:
        parser = YouTubeParser(client, api_key="")
        meta = await parser.parse("https://youtu.be/dQw4w9WgXcQ")

    assert meta.title == "Rick Roll"
    assert meta.site_name == "YouTube"
