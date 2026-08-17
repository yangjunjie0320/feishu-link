from __future__ import annotations

import re
import time
from typing import Any

import httpx
import pytest
import respx

from src.comment_analyzer import comments_from_raw
from src.tiktok_comments import (
    TikTokCommentError,
    aweme_id_from_url,
    comments_from_payloads,
    normalize_tiktok_comment,
    resolve_tiktok_url,
)


def _comment(cid: str, *, text: str = "nice", likes: int = 1, replies: int = 0) -> dict[str, Any]:
    return {
        "cid": cid,
        "text": text,
        "digg_count": likes,
        "reply_comment_total": replies,
        "reply_id": "0",
        "user": {"nickname": f"user{cid}", "unique_id": f"uid{cid}"},
    }


def _page(
    comments: list[dict[str, Any]],
    *,
    cursor: int,
    has_more: int = 1,
    total: int = 100,
    status_code: int = 0,
) -> dict[str, Any]:
    return {
        "status_code": status_code,
        "comments": comments,
        "cursor": cursor,
        "has_more": has_more,
        "total": total,
    }


def test_aweme_id_from_url_handles_video_photo_and_query() -> None:
    assert aweme_id_from_url("https://www.tiktok.com/@a/video/7653231150139182367") == (
        "7653231150139182367"
    )
    assert aweme_id_from_url("https://www.tiktok.com/@a/photo/123?is_from_webapp=1") == "123"
    assert aweme_id_from_url("https://www.tiktok.com/@someone") == ""


@respx.mock
async def test_resolve_tiktok_url_follows_short_link() -> None:
    full = "https://www.tiktok.com/@a/video/123"
    respx.head("https://vt.tiktok.com/ZSabc/").mock(
        return_value=httpx.Response(301, headers={"Location": full})
    )
    respx.head(full).mock(return_value=httpx.Response(200))

    async with httpx.AsyncClient() as client:
        assert await resolve_tiktok_url("https://vt.tiktok.com/ZSabc/", client) == full


@respx.mock
async def test_resolve_tiktok_url_returns_original_on_error() -> None:
    short = "https://vm.tiktok.com/ZSabc/"
    respx.head(short).mock(side_effect=httpx.ConnectError("boom"))

    async with httpx.AsyncClient() as client:
        assert await resolve_tiktok_url(short, client) == short


async def test_resolve_tiktok_url_leaves_full_links_alone() -> None:
    full = "https://www.tiktok.com/@a/video/123"
    async with httpx.AsyncClient() as client:
        assert await resolve_tiktok_url(full, client) == full


def test_normalize_maps_tiktok_fields_through_to_video_comment() -> None:
    """The generic converter knows none of TikTok's field names by default."""
    raw = {
        "cid": "7300",
        "text": "great video",
        "digg_count": 42,
        "reply_comment_total": 7,
        "reply_id": "0",
        "user": {"nickname": "Ada", "unique_id": "ada_l"},
    }

    comments = comments_from_raw([normalize_tiktok_comment(raw)], max_comments=10)

    assert len(comments) == 1
    comment = comments[0]
    assert comment.text == "great video"
    assert comment.like_count == 42
    assert comment.reply_count == 7
    assert comment.comment_id == "7300"
    assert comment.author == "Ada"
    assert comment.author_url == "https://www.tiktok.com/@ada_l"
    assert comment.parent_id == ""


def test_normalize_falls_back_to_unique_id_when_nickname_missing() -> None:
    raw = {"cid": "1", "text": "hi", "user": {"unique_id": "ada_l"}}

    normalized = normalize_tiktok_comment(raw)

    assert isinstance(normalized, dict)
    assert normalized["author"] == "@ada_l"


def test_normalize_treats_zero_reply_id_as_root_comment() -> None:
    root = normalize_tiktok_comment({"cid": "1", "text": "a", "reply_id": "0"})
    child = normalize_tiktok_comment({"cid": "2", "text": "b", "reply_id": "1"})

    assert isinstance(root, dict) and isinstance(child, dict)
    assert root["parent"] == ""
    assert child["parent"] == "1"


def test_normalize_drops_user_key_so_instagram_branch_cannot_claim_it() -> None:
    normalized = normalize_tiktok_comment(
        {"cid": "1", "text": "a", "user": {"username": "ig_name", "unique_id": "tt_name"}}
    )

    assert isinstance(normalized, dict)
    assert "user" not in normalized
    assert normalized["author_url"] == "https://www.tiktok.com/@tt_name"


def test_normalize_passes_through_non_dict() -> None:
    assert normalize_tiktok_comment("not a comment") == "not a comment"


def _netscape_cookie_file(tmp_path, lines: list[str]) -> str:
    path = tmp_path / "tiktok.txt"
    path.write_text("# Netscape HTTP Cookie File\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


async def test_seed_cookies_loads_exported_session(tmp_path, caplog) -> None:
    """A fresh profile is anonymous, and TikTok answers anonymous with an empty body."""
    from src.config import Settings
    from src.tiktok_comments import TikTokCommentClient

    cookie_file = _netscape_cookie_file(
        tmp_path,
        [
            ".tiktok.com\tTRUE\t/\tTRUE\t1900000000\tsessionid\tsecret",
            ".tiktok.com\tTRUE\t/\tTRUE\t1900000000\tssid_ucp_v1\talso-secret",
            ".example.com\tTRUE\t/\tTRUE\t1900000000\tunrelated\tnope",
        ],
    )
    added: list[list[dict]] = []

    class FakeContext:
        async def add_cookies(self, cookies):
            added.append(cookies)

    settings = Settings(platform_cookie_files={"tiktok": cookie_file})
    await TikTokCommentClient(settings)._seed_cookies(FakeContext())

    assert len(added) == 1
    names = {c["name"] for c in added[0]}
    assert names == {"sessionid", "ssid_ucp_v1"}


async def test_seed_cookies_warns_when_nothing_to_seed(tmp_path, caplog) -> None:
    from src.config import Settings
    from src.tiktok_comments import TikTokCommentClient

    class FakeContext:
        async def add_cookies(self, cookies):
            raise AssertionError("must not be called without cookies")

    settings = Settings(platform_cookie_files={"tiktok": str(tmp_path / "missing.txt")})
    with caplog.at_level("WARNING"):
        await TikTokCommentClient(settings)._seed_cookies(FakeContext())

    assert "no tiktok cookies to seed" in caplog.text




def _payload(
    comments: list[dict[str, Any]], *, total: int = 100, status_code: int = 0
) -> dict[str, Any]:
    return {"status_code": status_code, "comments": comments, "total": total}


def test_payloads_merge_across_intercepted_pages() -> None:
    collected, total = comments_from_payloads(
        [_payload([_comment("1"), _comment("2")]), _payload([_comment("3")])],
        max_comments=500,
    )

    assert len(collected) == 3
    assert total == 100


def test_payloads_dedupe_pinned_comment_repeated_across_pages() -> None:
    """The pinned comment comes back on every page and would eat the quota."""
    pinned = _comment("pin")
    collected, _ = comments_from_payloads(
        [_payload([pinned, _comment("1")]), _payload([pinned, _comment("2")])],
        max_comments=500,
    )

    assert len(collected) == 3


def test_payloads_respect_max_comments() -> None:
    collected, _ = comments_from_payloads(
        [_payload([_comment(str(i)) for i in range(20)])], max_comments=5
    )

    assert len(collected) == 5


def test_payloads_report_login_required_when_nothing_collected() -> None:
    with pytest.raises(TikTokCommentError, match="要求登录"):
        comments_from_payloads([_payload([], status_code=2154)], max_comments=500)


def test_payloads_report_item_unavailable() -> None:
    with pytest.raises(TikTokCommentError, match="不存在或已被删除"):
        comments_from_payloads([_payload([], status_code=2053)], max_comments=500)


def test_payloads_keep_data_when_a_later_page_errors() -> None:
    """A mid-scroll failure must not throw away what already arrived."""
    collected, _ = comments_from_payloads(
        [_payload([_comment("1")]), _payload([], status_code=2154)], max_comments=500
    )

    assert len(collected) == 1


def test_payloads_ignore_non_dict_entries() -> None:
    collected, _ = comments_from_payloads(
        ["<html>", None, _payload([_comment("1")])], max_comments=500
    )

    assert len(collected) == 1


def test_payloads_return_empty_without_error_when_no_comments() -> None:
    collected, total = comments_from_payloads([_payload([], total=0)], max_comments=500)

    assert collected == []
    assert total == 0


async def test_empty_body_is_reported_as_throttling_not_as_no_comments(monkeypatch) -> None:
    """An empty body is TikTok's throttling response, not an empty comment section."""
    from src.config import Settings
    from src.tiktok_comments import TikTokCommentClient

    client = TikTokCommentClient(Settings())

    async def fake_collect(self, page_url: str, *, max_comments: int):
        self._empty_responses = 2
        return []

    monkeypatch.setattr(TikTokCommentClient, "_collect_payloads", fake_collect)

    with pytest.raises(TikTokCommentError, match="限流"):
        await client.fetch_comments(
            "https://www.tiktok.com/@a/video/123", max_comments=10, deadline=time.monotonic() + 60
        )


async def test_panel_never_loading_is_distinct_from_throttling(monkeypatch) -> None:
    from src.config import Settings
    from src.tiktok_comments import TikTokCommentClient

    client = TikTokCommentClient(Settings())

    async def fake_collect(self, page_url: str, *, max_comments: int):
        self._empty_responses = 0
        return []

    monkeypatch.setattr(TikTokCommentClient, "_collect_payloads", fake_collect)

    with pytest.raises(TikTokCommentError, match="评论面板未加载"):
        await client.fetch_comments(
            "https://www.tiktok.com/@a/video/123", max_comments=10, deadline=time.monotonic() + 60
        )


def test_parse_abbreviated_count_handles_tiktok_display_formats() -> None:
    """Without this every DOM-path like count reads as 0 and heat sorting dies."""
    from src.tiktok_comments import parse_abbreviated_count

    assert parse_abbreviated_count("789") == 789
    assert parse_abbreviated_count("1.2K") == 1200
    assert parse_abbreviated_count("3.4M") == 3_400_000
    assert parse_abbreviated_count("1,234") == 1234
    assert parse_abbreviated_count("") == 0
    assert parse_abbreviated_count(None) == 0
    assert parse_abbreviated_count("赞") == 0


def test_normalize_dom_comment_maps_through_to_video_comment() -> None:
    from src.comment_analyzer import comments_from_raw
    from src.tiktok_comments import normalize_dom_comment

    scraped = [
        {"author": "Ada", "text": "great video", "likes": "1.2K"},
        {"author": "@bob_t", "text": "second", "likes": "5"},
    ]

    comments = comments_from_raw([normalize_dom_comment(c) for c in scraped], max_comments=10)

    assert [c.text for c in comments] == ["great video", "second"]
    assert comments[0].like_count == 1200
    assert comments[0].author_url == "https://www.tiktok.com/@Ada"
    # A leading @ must not become a doubled handle in the URL.
    assert comments[1].author_url == "https://www.tiktok.com/@bob_t"


def test_normalize_dom_comment_dedupes_without_ids() -> None:
    """The DOM carries no comment id, so dedupe leans on (author, text)."""
    from src.comment_analyzer import comments_from_raw
    from src.tiktok_comments import normalize_dom_comment

    scraped = [
        {"author": "Ada", "text": "pinned", "likes": "9K"},
        {"author": "Ada", "text": "pinned", "likes": "9K"},
        {"author": "Bob", "text": "other", "likes": "1"},
    ]

    comments = comments_from_raw([normalize_dom_comment(c) for c in scraped], max_comments=10)

    assert len(comments) == 2


async def test_proxy_is_passed_to_the_browser_only_when_configured(monkeypatch) -> None:
    """TikTok gates on IP reputation, so its browser can need its own egress."""
    from contextlib import asynccontextmanager

    from src.config import Settings
    from src.tiktok_comments import TikTokCommentClient

    seen: list[object] = []

    class _StopProbe(Exception):
        """Abort the launch once the kwargs have been captured."""

    @asynccontextmanager
    async def fake_context(profile_dir, **kwargs):
        seen.append(kwargs.get("proxy_server"))
        raise _StopProbe
        yield  # pragma: no cover

    import src.tiktok_comments as mod

    monkeypatch.setattr(mod, "persistent_context", fake_context)

    for proxy, expected in (("socks5://127.0.0.1:11080", "socks5://127.0.0.1:11080"), ("", None)):
        client = TikTokCommentClient(Settings(tiktok_comment_proxy=proxy))
        with pytest.raises(_StopProbe):
            await client._collect_payloads("https://www.tiktok.com/@a/video/1", max_comments=10)
        assert seen[-1] == expected


async def test_cooldown_rejects_once_the_window_is_full(monkeypatch) -> None:
    """A night of rapid retries is what degraded this path; the cap stops that."""
    from src.config import Settings
    from src.tiktok_comments import TikTokCommentClient

    calls = 0

    async def fake_collect(self, page_url: str, *, max_comments: int):
        nonlocal calls
        calls += 1
        return [{"status_code": 0, "comments": [_comment("1")], "total": 5}]

    monkeypatch.setattr(TikTokCommentClient, "_collect_payloads", fake_collect)
    settings = Settings(tiktok_comment_max_per_window=3, tiktok_comment_window_seconds=3600)
    client = TikTokCommentClient(settings)
    url = "https://www.tiktok.com/@a/video/123"

    for _ in range(3):
        await client.fetch_comments(url, max_comments=10, deadline=time.monotonic() + 60)
    assert calls == 3

    with pytest.raises(TikTokCommentError, match="冷却限制") as excinfo:
        await client.fetch_comments(url, max_comments=10, deadline=time.monotonic() + 60)
    # The message must name the wall-clock time it lifts, not just a duration:
    # a duration makes the reader do arithmetic against a stale timestamp.
    message = str(excinfo.value)
    assert re.search(r"北京时间 \d{2}:\d{2} 恢复", message), message
    assert "5 次/" not in message  # reflects the configured limit, not a default
    assert calls == 3, "被限流的请求不应触达浏览器"


async def test_cooldown_frees_slots_as_the_window_slides(monkeypatch) -> None:
    from src.config import Settings
    from src.tiktok_comments import TikTokCommentClient

    async def fake_collect(self, page_url: str, *, max_comments: int):
        return [{"status_code": 0, "comments": [_comment("1")], "total": 5}]

    monkeypatch.setattr(TikTokCommentClient, "_collect_payloads", fake_collect)
    settings = Settings(tiktok_comment_max_per_window=2, tiktok_comment_window_seconds=60)
    client = TikTokCommentClient(settings)
    url = "https://www.tiktok.com/@a/video/123"

    base = time.monotonic()
    monkeypatch.setattr("src.tiktok_comments.time.monotonic", lambda: base)
    for _ in range(2):
        await client.fetch_comments(url, max_comments=10, deadline=base + 60)
    with pytest.raises(TikTokCommentError, match="冷却限制"):
        await client.fetch_comments(url, max_comments=10, deadline=base + 60)

    # Past the window, the old entries expire and a slot opens again.
    monkeypatch.setattr("src.tiktok_comments.time.monotonic", lambda: base + 61)
    await client.fetch_comments(url, max_comments=10, deadline=base + 121)


def test_cooldown_disabled_when_limit_is_zero() -> None:
    from src.tiktok_comments import _quota_wait_seconds

    assert _quota_wait_seconds(0, 3600, 1000.0) == 0.0
