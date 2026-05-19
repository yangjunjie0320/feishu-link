import httpx
import respx

from feishu_link.config import Settings
from feishu_link.parsers.instagram_media_info import (
    InstagramMediaInfoParser,
    _shortcode_to_media_id,
)


def test_shortcode_to_media_id() -> None:
    assert _shortcode_to_media_id("DYfWbunGlNg") == 3899934464823415648


@respx.mock
async def test_instagram_media_info_uses_requested_carousel_image() -> None:
    media_id = _shortcode_to_media_id("DYfWbunGlNg")
    route = respx.get(
        f"https://www.instagram.com/api/v1/media/{media_id}/info/"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [{
                    "carousel_media": [
                        {
                            "image_versions2": {
                                "candidates": [{"url": "https://cdn.example.com/one.jpg"}],
                            },
                        },
                        {
                            "image_versions2": {
                                "candidates": [{"url": "https://cdn.example.com/two.jpg"}],
                            },
                        },
                    ],
                    "caption": {"text": "hello"},
                    "user": {"username": "beccu.studio", "full_name": "Rebecca"},
                    "like_count": 392,
                    "comment_count": 5,
                }],
            },
        )
    )

    async with httpx.AsyncClient() as client:
        meta = await InstagramMediaInfoParser(client, Settings()).parse(
            "https://www.instagram.com/p/DYfWbunGlNg/?img_index=2"
        )

    assert route.called
    assert meta.cover_url == "https://cdn.example.com/two.jpg"
    assert meta.description == "hello"
    assert meta.channel == "Rebecca"
    assert meta.like_count == 392
    assert meta.comment_count == 5
