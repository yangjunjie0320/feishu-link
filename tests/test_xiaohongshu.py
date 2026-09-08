from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio

from src.bibi_client import AuthenticationError, BibiAPIError, BibiClient
from src.bibi_models import SummaryResult
from src.card import build_card
from src.card_metadata import CardMetadataCache, card_result, content_key
from src.config import Settings
from src.dispatch import initial_card_result
from src.listener import CardActionEvent, MessageEvent
from src.parsers.base import MediaType, ParserError
from src.parsers.og_meta import OGMetaParser
from src.parsers.social_page import parse_page_metadata
from src.pipeline import Pipeline
from src.platforms import detect_platform, normalize_url
from src.summary_support import canonical_summary_url, supports_note_summary
from src.time_utils import now_utc
from src.url_extract import extract_urls

FIXTURES = Path(__file__).parent / "fixtures" / "xiaohongshu"
NOTE_ID = "6a9dfe020000000026038898"
URL = f"https://www.xiaohongshu.com/discovery/item/{NOTE_ID}?xsec_token=TEST"
ALIAS = f"https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=NEW&xsec_source=pc"
SHORT = "https://xhslink.com/a/Example"


def native_html(kind: str = "normal") -> str:
    data = json.loads((FIXTURES / "native-note.json").read_text())
    data["note"]["noteDetailMap"][NOTE_ID]["note"]["type"] = kind
    # Include the actual undefined literal and a quoted occurrence.
    return '<script>window.__INITIAL_STATE__=' + json.dumps(data)[:-1] + (
        ',"unused":undefined,"text":"undefined"}</script>'
    )


def test_identity_and_signed_urls() -> None:
    assert detect_platform(URL) == detect_platform(SHORT) == "xiaohongshu"
    assert normalize_url(URL) == normalize_url(ALIAS) == content_key(ALIAS)
    assert canonical_summary_url(URL) == URL
    assert canonical_summary_url(ALIAS) == ALIAS
    assert initial_card_result(SHORT).status != "unsupported"
    profile = "https://www.xiaohongshu.com/user/profile/123"
    assert initial_card_result(profile).status == "unsupported"
    assert detect_platform("https://xiaohongshu.com.evil.example/explore/123") == "web"
    assert extract_urls("text", json.dumps({"text": "分享笔记 " + SHORT}), Settings()) == [SHORT]


def test_native_note_uses_first_image_and_title() -> None:
    meta = parse_page_metadata(URL, native_html())
    assert meta.media_type == MediaType.ARTICLE
    assert meta.title == "一个大学教授与新生家长的对话"
    assert meta.channel == "启航合伙人"
    assert meta.cover_url.endswith("image-1.jpg")
    assert all("-1.jpg" in url for url in meta.cover_candidates)
    assert meta.canonical_url == URL
    assert supports_note_summary(meta)
    card = json.loads(build_card(meta, "image_key"))
    encoded = json.dumps(card, ensure_ascii=False)
    assert "总结笔记" in encoded and "总结视频" not in encoded
    assert "分析评论" not in encoded
    assert "小红书" in encoded and "image_key" in encoded
    assert "总结笔记" in build_card(meta, None)


@pytest.mark.parametrize("kind", ["video", "unknown", ""])
def test_non_photo_note_has_no_summary(kind: str) -> None:
    meta = parse_page_metadata(URL, native_html(kind))
    assert not supports_note_summary(meta)
    assert "summarize_video" not in build_card(meta, None)


@pytest.mark.parametrize("text,reason", [
    ("扫码登录", "auth"), ("安全验证", "challenge"), ("笔记已删除", "deleted"),
])
def test_unavailable_pages_fail(text: str, reason: str) -> None:
    with pytest.raises(ParserError, match=reason):
        parse_page_metadata(URL, f"<h1>{text}</h1>")


def test_recommendation_is_not_target() -> None:
    other = URL.replace(NOTE_ID, "0" * 24)
    with pytest.raises(ParserError, match="target_mismatch"):
        parse_page_metadata(URL, native_html(), final_url=other)
    with pytest.raises(ParserError, match="no_content"):
        parse_page_metadata(other, native_html())


async def test_http_short_link_resolves_without_og_override() -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        if str(request.url) == SHORT:
            return httpx.Response(302, headers={"location": URL})
        return httpx.Response(200, text=native_html() + '<meta property="og:type" content="video">')
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        meta = await OGMetaParser(client).parse(SHORT)
    assert meta.source_url == SHORT and meta.canonical_url == URL
    assert supports_note_summary(meta)


async def test_card_cache_shares_aliases() -> None:
    factory = AsyncMock(return_value=card_result(parse_page_metadata(URL, native_html())))
    cache = CardMetadataCache()
    await asyncio.gather(cache.get(URL, factory), cache.get(ALIAS, factory))
    factory.assert_awaited_once()


async def test_cached_short_link_and_unsigned_alias_keep_access_signature() -> None:
    meta = parse_page_metadata(SHORT, native_html(), final_url=URL)
    factory = AsyncMock(return_value=card_result(meta))
    cache = CardMetadataCache()
    await cache.get(SHORT, factory)
    unsigned = URL.split("?", 1)[0]
    cached = await cache.get(unsigned, factory)
    assert cached.metadata.canonical_url == URL
    refreshed = await cache.get(ALIAS, factory)
    assert refreshed.metadata.canonical_url == ALIAS
    factory.assert_awaited_once()


def test_origin_redirect_retains_signature() -> None:
    meta = parse_page_metadata(URL, native_html(), final_url=URL.split("?", 1)[0])
    assert meta.canonical_url == URL


async def test_short_alias_invalidation_removes_long_alias() -> None:
    factory = AsyncMock(return_value=card_result(
        parse_page_metadata(SHORT, native_html(), final_url=URL),
    ))
    cache = CardMetadataCache()
    await cache.get(SHORT, factory)
    cache.invalidate(SHORT)
    await cache.get(URL, factory)
    assert factory.await_count == 2


def bibi_client() -> BibiClient:
    return BibiClient(Settings(cookie_file="", bibigpt_access_mode="browser"))


async def test_client_uses_verified_image_task_without_subtitle_preparation() -> None:
    client = bibi_client()
    detail = json.loads((FIXTURES / "bibigpt-detail.json").read_text())
    client._browser_fetch_json = AsyncMock(side_effect=[
        {"result": {"data": {"json": detail}}},
        {"result": {"data": {"json": {"id": detail["dbId"], "summaryText": "图文要点"}}}},
    ])
    client._prepare_content = AsyncMock()
    client._summarize_browser = AsyncMock()
    result = await client.summarize(URL)
    assert result.content == "图文要点" and result.content_id == detail["dbId"]
    calls = client._browser_fetch_json.call_args_list
    query = json.loads(parse_qs(urlsplit(calls[0].args[0]).query)["input"][0])["json"]
    assert query["url"] == URL and query["skipSubtitleTask"] is True
    assert "contentPipeline.summarize" in calls[1].args[0]
    assert calls[1].args[1]["0"]["json"]["promptConfig"]["showTimestamp"] is False
    assert calls[1].args[1]["0"]["json"]["promptConfig"]["isRefresh"] is True
    assert calls[1].args[1]["0"]["json"]["url"] == URL
    client._prepare_content.assert_not_awaited()
    client._summarize_browser.assert_not_awaited()


@pytest.mark.parametrize("field,value", [
    ("images", []), ("audioUrl", "https://example.com/a.mp4"),
    ("playUrl", "https://example.com/a.mp4"), ("id", "wrong"), ("dbId", ""),
])
async def test_client_refuses_text_only_or_mismatched_detail(field: str, value: Any) -> None:
    client = bibi_client()
    detail = json.loads((FIXTURES / "bibigpt-detail.json").read_text())
    detail[field] = value
    client._browser_fetch_json = AsyncMock(return_value={"result": {"data": {"json": detail}}})
    with pytest.raises(BibiAPIError):
        await client.summarize(URL)
    assert client._browser_fetch_json.await_count == 1


async def test_client_auth_failure_never_falls_back() -> None:
    client = bibi_client()
    client._browser_fetch_json = AsyncMock(side_effect=AuthenticationError(401, "expired"))
    with pytest.raises(AuthenticationError):
        await client.summarize(URL)
    assert client._browser_fetch_json.await_count == 1


@pytest_asyncio.fixture
async def pipeline() -> AsyncIterator[Pipeline]:
    archive = MagicMock()
    archive.find_bibigpt_content_id = AsyncMock(return_value="old-text-only-content")
    instance = Pipeline(Settings(), MagicMock(), archive=archive)
    instance._typing_sender.hold = MagicMock()
    parsed = card_result(parse_page_metadata(URL, native_html()))
    instance._prepare_card = AsyncMock(return_value=(parsed, None))
    instance._dispatcher.parse_card = AsyncMock(return_value=parsed)
    instance._sender.send = AsyncMock(return_value=True)
    instance._text_sender.send = AsyncMock(return_value=True)
    instance._translator.ensure_chinese_markdown_summary = AsyncMock(
        side_effect=lambda text, **kw: text,
    )
    instance._bibi_client.summarize = AsyncMock(return_value=SummaryResult.from_web_response(
        {"summary": "图片中的要点", "contentId": "content-id"}, video_url=URL,
    ))
    instance._bibi_client.summarize_cached = AsyncMock()
    instance._try_send_bibigpt_chapter_summary = AsyncMock()
    instance._try_send_video = AsyncMock()
    try:
        yield instance
    finally:
        await instance._http.aclose()


def action(url: str = URL, message_id: str = "card") -> CardActionEvent:
    return CardActionEvent(action="summarize_video", source_url=url,
                           message_id=message_id, chat_id="chat", operator_open_id="user")


async def test_card_then_button_summary_no_mention_or_video(pipeline: Pipeline) -> None:
    event = MessageEvent(message_id="message", chat_id="chat", chat_type="p2p", sender_id="user",
                         message_type="text", content=json.dumps({"text": URL}),
                         timestamp_utc=now_utc(), mentions=[])
    await pipeline._process_url(URL, event, is_bot_mentioned=True, manual_download=False)
    pipeline._bibi_client.summarize.assert_not_awaited()
    assert "总结笔记" in pipeline._sender.send.call_args.args[0]
    await pipeline._handle_card_action(action())
    pipeline._bibi_client.summarize.assert_awaited_once()
    pipeline._bibi_client.summarize_cached.assert_not_awaited()
    pipeline._try_send_video.assert_not_awaited()
    pipeline._try_send_bibigpt_chapter_summary.assert_not_awaited()
    assert pipeline._archive.enqueue.call_args.args[0].bibigpt_url.endswith("/content/content-id")
    assert pipeline._active_summary_messages == set()


async def test_button_aliases_share_inflight_and_repeat_is_ignored(pipeline: Pipeline) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    original = pipeline._bibi_client.summarize.return_value

    async def generate(*args: Any, **kwargs: Any) -> SummaryResult:
        entered.set()
        await release.wait()
        return original

    pipeline._bibi_client.summarize.side_effect = generate
    first = asyncio.create_task(pipeline._handle_card_action(action()))
    await entered.wait()
    await pipeline._handle_card_action(action())
    other = asyncio.create_task(pipeline._handle_card_action(action(ALIAS, "other")))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, other)
    pipeline._bibi_client.summarize.assert_awaited_once()
    assert pipeline._sender.send.await_count == 2


async def test_failure_cleans_state_and_preserves_card(pipeline: Pipeline) -> None:
    pipeline._bibi_client.summarize.side_effect = BibiAPIError(401, "expired")
    await pipeline._handle_card_action(action())
    pipeline._text_sender.send.assert_awaited_once()
    pipeline._sender.send.assert_not_awaited()
    assert pipeline._active_summary_messages == set()


async def test_old_callback_revalidates_video(pipeline: Pipeline) -> None:
    pipeline._dispatcher.parse_card.return_value = card_result(
        parse_page_metadata(URL, native_html("video")),
    )
    await pipeline._handle_card_action(action())
    pipeline._bibi_client.summarize.assert_not_awaited()
    assert "图文" in pipeline._text_sender.send.call_args.args[0]


@pytest.mark.parametrize("result", [
    {"id": "other", "summaryText": "wrong note"},
    {"id": "0f790168-4d9b-4d67-81ca-5638029249cf", "summaryText": ""},
    {"id": "0f790168-4d9b-4d67-81ca-5638029249cf", "summaryText": None},
])
async def test_image_task_rejects_wrong_or_empty_result(result: dict[str, Any]) -> None:
    client = bibi_client()
    detail = json.loads((FIXTURES / "bibigpt-detail.json").read_text())
    client._browser_fetch_json = AsyncMock(side_effect=[
        {"result": {"data": {"json": detail}}},
        {"result": {"data": {"json": result}}},
    ])
    with pytest.raises(BibiAPIError, match="empty or mismatched"):
        await client.summarize(URL)
    assert client._browser_fetch_json.await_count == 2
