import json

import httpx
import pytest
import respx

from src.card_metadata import card_result
from src.parsers.base import CardStatus, ParserError
from src.parsers.og_meta import OGMetaParser

OG_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta property="og:title" content="Test Article" />
  <meta property="og:description" content="A description" />
  <meta property="og:image" content="https://example.com/img.jpg" />
  <meta property="og:site_name" content="Example Site" />
</head>
<body></body>
</html>"""

PLAIN_HTML = """<!DOCTYPE html>
<html><head><title>Plain Title</title></head><body></body></html>"""


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient()


@respx.mock
async def test_og_meta_full(client: httpx.AsyncClient) -> None:
    respx.get("https://example.com/article").mock(
        return_value=httpx.Response(200, text=OG_HTML)
    )
    parser = OGMetaParser(client)
    meta = await parser.parse("https://example.com/article")
    assert meta.title == "Test Article"
    assert meta.description == "A description"
    assert meta.cover_url == "https://example.com/img.jpg"
    assert meta.site_name == "Example Site"
    assert meta.source_url == "https://example.com/article"


@respx.mock
async def test_og_meta_fallback_to_title(client: httpx.AsyncClient) -> None:
    respx.get("https://example.com/plain").mock(
        return_value=httpx.Response(200, text=PLAIN_HTML)
    )
    parser = OGMetaParser(client)
    meta = await parser.parse("https://example.com/plain")
    assert meta.title == "Plain Title"


@respx.mock
async def test_og_meta_http_error(client: httpx.AsyncClient) -> None:
    respx.get("https://example.com/gone").mock(
        return_value=httpx.Response(404)
    )
    parser = OGMetaParser(client)
    with pytest.raises(ParserError) as exc_info:
        await parser.parse("https://example.com/gone")
    assert "404" in exc_info.value.reason


@respx.mock
async def test_og_meta_network_error(client: httpx.AsyncClient) -> None:
    respx.get("https://example.com/error").mock(
        side_effect=httpx.ConnectError("refused")
    )
    parser = OGMetaParser(client)
    with pytest.raises(ParserError):
        await parser.parse("https://example.com/error")


@pytest.mark.parametrize("known_carousel", [False, True])
async def test_instagram_og_cannot_restore_an_unverified_default_cover(
    known_carousel: bool,
) -> None:
    url = "https://www.instagram.com/p/ABC_def/?img_index=2"
    item = {"code": "ABC_def", "caption": {"text": "Real caption"},
            "user": {"username": "writer"}, "display_url": "https://cdn.example/default"}
    if known_carousel:
        item["carousel_media"] = [
            {"display_url": "https://cdn.example/default"},
            {"display_url": "https://cdn.example/second"},
        ]
    html = OG_HTML + '<script type="application/json">' + json.dumps(item) + '</script>'
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=html))
    async with httpx.AsyncClient(transport=transport) as client:
        meta = await OGMetaParser(client).parse(url)
    assert meta.channel == "writer"
    if known_carousel:
        assert meta.cover_candidates == ["https://cdn.example/second"]
        assert card_result(meta).status == CardStatus.COMPLETE
    else:
        assert meta.cover_url == ""
        assert meta.cover_candidates == []
        assert card_result(meta).status == CardStatus.PARTIAL


@pytest.mark.parametrize("recommendation_redirect", [False, True])
async def test_short_link_http_redirects_preserve_the_first_post_identity(
    recommendation_redirect: bool,
) -> None:
    short = "https://v.douyin.com/share/"
    target = "https://www.douyin.com/note/1234567890"
    recommended = "https://www.douyin.com/jingxuan?modal_id=9999999999"
    payload = {
        "aweme_id": "9999999999" if recommendation_redirect else "1234567890",
        "desc": "Wrong recommendation" if recommendation_redirect else "Original caption",
        "images": [{"url_list": ["https://cdn.example/image"]}],
    }

    def response(request: httpx.Request) -> httpx.Response:
        if str(request.url) == short:
            return httpx.Response(302, headers={"Location": "//www.douyin.com/note/1234567890"})
        if str(request.url) == target and recommendation_redirect:
            return httpx.Response(302, headers={"Location": recommended})
        return httpx.Response(
            200, text='<script type="application/json">' + json.dumps(payload) + '</script>',
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        parser = OGMetaParser(client)
        if recommendation_redirect:
            with pytest.raises(ParserError, match="target_mismatch") as exc:
                await parser.parse(short)
            assert exc.value.url == short
        else:
            meta = await parser.parse(short)
            assert meta.source_url == short
            assert meta.canonical_url == target
            assert meta.title == "Original caption"
