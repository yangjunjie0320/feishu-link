from pathlib import Path

import httpx
import pytest
import respx

from src.card_metadata import card_result
from src.config import Settings
from src.parsers.base import CardStatus
from src.parsers.instagram_media_info import (
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


@pytest.mark.parametrize("carousel", [None, [{"image_versions2": {
    "candidates": [{"url": "https://cdn.example.com/first.jpg"}],
}}], [{"image_versions2": {
    "candidates": [{"url": "https://cdn.example.com/first.jpg"}],
}}, {}]])
async def test_instagram_media_info_does_not_substitute_first_image_for_requested_slide(
    carousel: list | None,
) -> None:
    item = {
        "caption": {"text": "Actual caption"}, "user": {"username": "writer"},
        "image_versions2": {"candidates": [{"url": "https://cdn.example.com/default.jpg"}]},
        "carousel_media": carousel,
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"items": [item]}))
    async with httpx.AsyncClient(transport=transport) as client:
        meta = await InstagramMediaInfoParser(client, Settings()).parse(
            "https://www.instagram.com/p/DYfWbunGlNg/?img_index=2"
        )
    assert meta.description == "Actual caption"
    assert meta.channel == "writer"
    assert meta.cover_url == ""
    assert meta.cover_candidates == []
    assert card_result(meta).status == CardStatus.PARTIAL


@respx.mock
async def test_instagram_media_info_sends_instagram_cookie(tmp_path: Path) -> None:
    cookie_file = tmp_path / "instagram.txt"
    cookie_file.write_text(
        "\n".join([
            "# Netscape HTTP Cookie File",
            ".instagram.com\tTRUE\t/\tTRUE\t1800000000\tsessionid\tabc",
        ]),
        encoding="utf-8",
    )
    media_id = _shortcode_to_media_id("DYfWbunGlNg")
    route = respx.get(
        f"https://www.instagram.com/api/v1/media/{media_id}/info/"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [{
                    "image_versions2": {
                        "candidates": [{"url": "https://cdn.example.com/one.jpg"}],
                    },
                    "user": {"username": "beccu.studio"},
                }],
            },
        )
    )
    settings = Settings(platform_cookie_files={"instagram": str(cookie_file)})

    async with httpx.AsyncClient() as client:
        await InstagramMediaInfoParser(client, settings).parse(
            "https://www.instagram.com/p/DYfWbunGlNg/"
        )

    assert route.calls.last.request.headers["Cookie"] == "sessionid=abc"
