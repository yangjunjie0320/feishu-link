from collections.abc import Awaitable, Callable
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from src.card_metadata import CardSourceError, card_result
from src.config import Settings
from src.dispatch import Dispatcher
from src.parsers.base import CardStatus, LinkMetadata, MediaType, ParserError
from src.parsers.instagram_media_info import InstagramMediaInfoParser
from src.parsers.lightweight_oembed import LightweightOEmbedParser
from src.parsers.og_meta import OGMetaParser
from src.parsers.x_graphql import XGraphQLParser
from src.parsers.x_oembed import XOEmbedParser
from src.parsers.youtube import YouTubeParser, extract_video_id

YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
INSTAGRAM = "https://www.instagram.com/reel/abc/"
X = "https://twitter.com/author/status/123"


def _parse(
    name: str, client: httpx.AsyncClient,
) -> tuple[Callable[[str], Awaitable[LinkMetadata]], str]:
    settings = Settings(cookie_file="tests/fixtures/x-cookie.txt")
    if name == "youtube":
        return YouTubeParser(client, api_key="test").parse_api, YOUTUBE
    if name == "instagram":
        return InstagramMediaInfoParser(client, settings).parse, INSTAGRAM
    if name == "x_graphql":
        return XGraphQLParser(client, settings).parse, X
    if name == "x_oembed":
        return XOEmbedParser(client).parse, X
    return LightweightOEmbedParser(client).parse, YOUTUBE


@pytest.mark.parametrize("name", ["youtube", "instagram", "x_graphql", "x_oembed", "oembed"])
@pytest.mark.parametrize("body", ["<html>Log in</html>", "null", "[]", '{"data":null}'])
async def test_unexpected_200_response_is_a_parser_error(name: str, body: str) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body))
    async with httpx.AsyncClient(transport=transport) as client:
        read, url = _parse(name, client)
        with pytest.raises(ParserError):
            await read(url)


@pytest.mark.parametrize(("status", "body", "kind"), [
    (401, "", "auth"),
    (403, '{"message":"login_required"}', "auth"),
    (403, "Forbidden", "content"),
    (429, "Too many requests", "rate_limit"),
    (503, "Temporary failure", "network"),
    (200, '{"errors":[{"message":"Could not authenticate you"}]}', "auth"),
    (200, '{"errors":[{"message":"Rate limit exceeded"}]}', "rate_limit"),
    (400, '{"message":"checkpoint_required","status":"fail"}', "challenge"),
    (200, '{"message":"checkpoint_required","status":"fail"}', "challenge"),
])
async def test_x_response_distinguishes_auth_rate_limit_and_network(
    status: int, body: str, kind: str,
) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(status, text=body))
    async with httpx.AsyncClient(transport=transport) as client:
        read, url = _parse("x_graphql", client)
        with pytest.raises(CardSourceError) as error:
            await read(url)
    assert error.value.kind == kind


async def test_instagram_keeps_caption_without_images_and_marks_reels_as_video() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "items": [{"media_type": 2, "caption": {"text": "Real caption"},
                   "user": {"username": "creator"}, "video_duration": 12.7}],
    }))
    async with httpx.AsyncClient(transport=transport) as client:
        meta = await InstagramMediaInfoParser(client, Settings()).parse(INSTAGRAM)
    assert meta.description == "Real caption"
    assert meta.cover_url == ""
    assert meta.has_visual is True
    assert meta.content_verified is True
    assert meta.media_type == MediaType.VIDEO
    assert meta.duration_seconds == 12
    assert card_result(meta).status == CardStatus.PARTIAL


async def test_x_twitter_input_sends_x_domain_cookie_and_accepts_plain_text() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "data": {"tweetResult": {"result": {
                "rest_id": "123", "legacy": {"full_text": "Real plain text"},
            }}},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        read, url = _parse("x_graphql", client)
        meta = await read(url)
    assert requests[0].url.host == "x.com"
    assert "auth_token=" in requests[0].headers["cookie"]
    assert requests[0].headers["x-csrf-token"]
    assert meta.has_visual is False
    assert card_result(meta).status == CardStatus.COMPLETE


async def test_youtube_og_uses_configured_cookie(tmp_path: Path) -> None:
    cookie = tmp_path / "youtube.txt"
    cookie.write_text(
        "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2000000000\tSAPISID\ttest\n",
    )
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text='<meta property="og:title" content="Real title">')

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        parser = YouTubeParser(
            client, settings=Settings(platform_cookie_files={"youtube": str(cookie)}),
        )
        assert (await parser.parse(YOUTUBE)).title == "Real title"
    assert requests[0].headers["cookie"] == "SAPISID=test"


@pytest.mark.parametrize(("url", "expected"), [
    ("https://youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://evil.example/youtube.com/watch?v=dQw4w9WgXcQ", None),
    ("https://youtube.com/watch?v=dQw4w9WgXcQextra", None),
])
def test_youtube_identity_uses_real_host_and_exact_id(url: str, expected: str | None) -> None:
    assert extract_video_id(url) == expected


@pytest.mark.parametrize("reason", [
    "target_mismatch: recommended post", "auth: login page", "challenge: verify request",
])
async def test_og_does_not_rescue_another_post_or_a_challenge(
    reason: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject(*args: object, **kwargs: object) -> LinkMetadata:
        raise ParserError(YOUTUBE, reason)

    monkeypatch.setattr("src.parsers.social_page.parse_page_metadata", reject)
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, text='<meta property="og:title" content="Recommended content">',
    ))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ParserError, match=reason):
            await OGMetaParser(client).parse(YOUTUBE)


async def test_tiktok_oembed_preserves_caption_and_rejects_other_video() -> None:
    url = "https://www.tiktok.com/@creator/video/123"
    payload = {
        "title": "Actual caption #tag", "author_name": "Creator", "type": "video",
        "thumbnail_url": "https://cdn.example/image?sig=123",
        "html": f'<blockquote cite="{url}"><p>Actual caption #tag</p></blockquote>',
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    async with httpx.AsyncClient(transport=transport) as client:
        parser = LightweightOEmbedParser(client)
        meta = await parser.parse(url)
        assert meta.description == payload["title"]
        assert meta.cover_url.endswith("?sig=123")
        assert card_result(meta).status == CardStatus.COMPLETE
        payload["html"] = '<blockquote cite="https://www.tiktok.com/@creator/video/999">'
        with pytest.raises(ParserError, match="another video"):
            await parser.parse(url)


async def test_api_rejects_a_different_video_and_does_not_hide_error_in_og() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "items": [{"id": "abcdefghijk", "snippet": {"title": "Wrong title"}}],
    }))
    async with httpx.AsyncClient(transport=transport) as client:
        parser = YouTubeParser(client, api_key="test")
        parser._og_parser = AsyncMock()
        with pytest.raises(ParserError, match="target_mismatch"):
            await parser.parse_api(YOUTUBE)
        parser._og_parser.parse.assert_not_called()


@respx.mock
async def test_instagram_checkpoint_falls_back_to_public_page_without_cookie_jar(
    tmp_path: Path,
) -> None:
    cookie_file = tmp_path / "instagram.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n.instagram.com\tTRUE\t/\tTRUE\t2000000000\tsessionid\told\n",
    )
    original = cookie_file.read_text()
    # abc's shortcode ID is 108252; the exact conversion is already covered.
    respx.route(path__regex=r"/api/v1/media/\d+/info/").mock(return_value=httpx.Response(
        400, json={"message": "checkpoint_required", "status": "fail"},
    ))
    requests: list[httpx.Request] = []

    def page(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.headers.get("cookie"):
            return httpx.Response(400, json={"message": "checkpoint_required"})
        assert request.headers["user-agent"].startswith("python-httpx/")
        return httpx.Response(200, text=(
            '<meta property="og:title" content="Actual public reel">'
            '<meta property="og:description" content="A public caption">'
            '<meta property="og:image" content="https://cdn.example/public.jpg">'
        ))

    respx.get(INSTAGRAM).mock(side_effect=page)
    settings = Settings(cookie_refresh_enabled=False,
                        platform_cookie_files={"instagram": str(cookie_file)})
    async with httpx.AsyncClient() as client:
        client.cookies.set("sessionid", "shared-old", domain=".instagram.com")
        dispatcher = Dispatcher(settings, client)
        dispatcher._browser = AsyncMock()
        result = await dispatcher.parse_card(INSTAGRAM)
        assert result.status == CardStatus.COMPLETE
        assert result.sources == ["instagram_public_page"]
        assert result.metadata.description == "A public caption"
        assert result.metadata.cover_url == "https://cdn.example/public.jpg"
        dispatcher._browser.parse.assert_not_called()
        assert client.cookies.get("sessionid") == "shared-old"
    assert requests[0].headers.get("cookie")
    assert "cookie" not in requests[1].headers
    assert cookie_file.read_text() == original
