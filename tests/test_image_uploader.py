import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import lark_oapi as lark
import pytest
import respx

from src.image_uploader import _normalize_cover_url, upload_cover, upload_cover_with_result


def test_normalize_cover_url_accepts_protocol_relative_urls() -> None:
    url = "//i0.hdslb.com/bfs/archive/example.jpg@100w_100h_1c.png"

    assert _normalize_cover_url(url) == (
        "https://i0.hdslb.com/bfs/archive/example.jpg@100w_100h_1c.png"
    )


def test_normalize_cover_url_strips_whitespace() -> None:
    assert _normalize_cover_url("  https://example.com/cover.jpg  ") == (
        "https://example.com/cover.jpg"
    )


def test_normalize_cover_url_returns_empty_for_blank_url() -> None:
    assert _normalize_cover_url("   ") == ""


def _image_client(upload: AsyncMock | None = None):
    if upload is None:
        upload = AsyncMock(return_value=SimpleNamespace(
            success=lambda: True, data=SimpleNamespace(image_key="img_cover")
        ))
    return SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(image=SimpleNamespace(
        acreate=upload
    ))))


@respx.mock
async def test_cover_tries_fallback_without_losing_signature_or_source_headers(caplog) -> None:
    first = "https://cdn.example/expired.jpg?signature=secret_signature"
    second = "https://cdn.example/available.jpg?signature=another_signature"
    expired = respx.get(first).mock(return_value=httpx.Response(403))
    available = respx.get(second).mock(return_value=httpx.Response(
        200, content=b"image content", headers={"content-type": "image/jpeg"}
    ))
    client = _image_client()
    with caplog.at_level(logging.WARNING, logger="src.image_uploader"):
        async with httpx.AsyncClient() as http:
            key = await upload_cover(
                first, client, http,
                candidates=[first, second],
                headers={"Referer": "https://source.example/", "Cookie": "private_cookie"},
            )

    assert key == "img_cover"
    assert expired.call_count == 1
    assert available.calls.last.request.url.query == b"signature=another_signature"
    assert available.calls.last.request.headers["referer"] == "https://source.example/"
    client.im.v1.image.acreate.assert_awaited_once()
    assert "secret_signature" not in caplog.text
    assert "private_cookie" not in caplog.text
    assert "fetch HTTP 403" in caplog.text


@respx.mock
async def test_cover_stops_after_three_distinct_candidates() -> None:
    urls = [f"https://cdn.example/{index}.jpg" for index in range(4)]
    routes = [respx.get(url).mock(return_value=httpx.Response(404)) for url in urls]
    client = _image_client()
    async with httpx.AsyncClient() as http:
        result = await upload_cover_with_result(urls[0], client, http, candidates=urls)

    assert result.image_key is None
    assert result.attempted == 3
    assert [route.call_count for route in routes] == [1, 1, 1, 0]
    client.im.v1.image.acreate.assert_not_awaited()


@respx.mock
async def test_cover_rejects_html_before_image_upload() -> None:
    respx.get("https://cdn.example/login").mock(return_value=httpx.Response(
        200, text="<html>login required</html>", headers={"content-type": "text/html"}
    ))
    respx.get("https://cdn.example/image").mock(return_value=httpx.Response(
        200, content=b"image content", headers={"content-type": "image/png"}
    ))
    client = _image_client()
    async with httpx.AsyncClient() as http:
        result = await upload_cover_with_result(
            "https://cdn.example/login", client, http,
            candidates=["https://cdn.example/image"],
        )

    assert result.image_key == "img_cover"
    assert result.attempted == 2
    client.im.v1.image.acreate.assert_awaited_once()


@pytest.mark.parametrize("stage", ["fetch", "upload"])
async def test_cover_total_budget_cancels_pending_io(stage: str) -> None:
    cancelled = asyncio.Event()

    async def stalled(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def fetch(request):
        if stage == "fetch":
            await stalled()
        return httpx.Response(200, content=b"image", headers={"content-type": "image/jpeg"})

    client = _image_client(AsyncMock(side_effect=stalled) if stage == "upload" else None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as http:
        result = await asyncio.wait_for(
            upload_cover_with_result("https://cdn.example/image", client, http, timeout=0.05),
            timeout=0.5,
        )

    assert result.image_key is None
    assert result.status == "timeout"
    assert cancelled.is_set()


@respx.mock
async def test_image_sdk_upload_and_cold_auth_are_async(monkeypatch) -> None:
    def reject_sync_http(*args, **kwargs):
        raise AssertionError("synchronous image or token request")

    monkeypatch.setattr("requests.request", reject_sync_http)
    respx.get("https://cdn.example/image").mock(return_value=httpx.Response(
        200, content=b"image content", headers={"content-type": "image/png"}
    ))
    respx.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal").mock(
        return_value=httpx.Response(200, json={
            "code": 0, "tenant_access_token": "test_image_token", "expire": 3600,
        })
    )
    upload = respx.post("https://open.feishu.cn/open-apis/im/v1/images").mock(
        return_value=httpx.Response(200, json={"code": 0, "data": {"image_key": "img_async"}})
    )
    client = lark.Client.builder().app_id("image_app").app_secret("image_secret").build()
    async with httpx.AsyncClient() as http:
        result = await upload_cover_with_result("https://cdn.example/image", client, http)

    assert result.image_key == "img_async"
    assert upload.calls.last.request.headers["Authorization"] == "Bearer test_image_token"
    assert b"image content" in upload.calls.last.request.content
