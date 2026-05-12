import httpx
import pytest
import respx

from feishu_link.parsers.base import ParserError
from feishu_link.parsers.og_meta import OGMetaParser

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
