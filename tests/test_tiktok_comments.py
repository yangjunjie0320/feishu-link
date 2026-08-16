from __future__ import annotations

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


def test_seed_browser_state_copies_local_storage_and_tiktok_idb(tmp_path) -> None:
    """Cookies alone leave the profile looking like a new device."""
    from src.config import Settings
    from src.tiktok_comments import TikTokCommentClient

    chrome = tmp_path / "chrome"
    (chrome / "Local Storage" / "leveldb").mkdir(parents=True)
    (chrome / "Local Storage" / "leveldb" / "000.ldb").write_text("ls", encoding="utf-8")
    (chrome / "Session Storage").mkdir()
    (chrome / "Session Storage" / "s.ldb").write_text("ss", encoding="utf-8")
    idb = chrome / "IndexedDB"
    (idb / "https_www.tiktok.com_0.indexeddb.leveldb").mkdir(parents=True)
    (idb / "https_www.tiktok.com_0.indexeddb.leveldb" / "1.ldb").write_text("tt", encoding="utf-8")
    (idb / "https_www.example.com_0.indexeddb.leveldb").mkdir(parents=True)
    (idb / "https_www.example.com_0.indexeddb.leveldb" / "2.ldb").write_text("no", encoding="utf-8")

    profile = tmp_path / "profile"
    settings = Settings(
        tiktok_comment_browser_profile_dir=str(profile),
        tiktok_comment_chrome_profile_dir=str(chrome),
    )
    TikTokCommentClient(settings)._seed_browser_state()

    default = profile / "Default"
    assert (default / "Local Storage" / "leveldb" / "000.ldb").exists()
    assert (default / "Session Storage" / "s.ldb").exists()
    assert (default / "IndexedDB" / "https_www.tiktok.com_0.indexeddb.leveldb" / "1.ldb").exists()
    # Other sites' IndexedDB is large and irrelevant.
    assert not (default / "IndexedDB" / "https_www.example.com_0.indexeddb.leveldb").exists()


def test_seed_browser_state_runs_once_per_profile(tmp_path) -> None:
    from src.config import Settings
    from src.tiktok_comments import TikTokCommentClient

    chrome = tmp_path / "chrome"
    (chrome / "Local Storage").mkdir(parents=True)
    (chrome / "Local Storage" / "a.ldb").write_text("first", encoding="utf-8")

    profile = tmp_path / "profile"
    settings = Settings(
        tiktok_comment_browser_profile_dir=str(profile),
        tiktok_comment_chrome_profile_dir=str(chrome),
    )
    client = TikTokCommentClient(settings)
    client._seed_browser_state()

    # A second call must not re-copy over live profile state.
    (chrome / "Local Storage" / "a.ldb").write_text("changed", encoding="utf-8")
    client._seed_browser_state()

    assert (profile / "Default" / "Local Storage" / "a.ldb").read_text(encoding="utf-8") == "first"


def test_seed_browser_state_skips_when_disabled_or_source_missing(tmp_path) -> None:
    from src.config import Settings
    from src.tiktok_comments import TikTokCommentClient

    profile = tmp_path / "profile"
    disabled = Settings(
        tiktok_comment_browser_profile_dir=str(profile),
        tiktok_comment_chrome_profile_dir=str(tmp_path / "chrome"),
        tiktok_comment_seed_browser_state=False,
    )
    TikTokCommentClient(disabled)._seed_browser_state()
    assert not profile.exists()

    missing_source = Settings(
        tiktok_comment_browser_profile_dir=str(profile),
        tiktok_comment_chrome_profile_dir=str(tmp_path / "nope"),
    )
    TikTokCommentClient(missing_source)._seed_browser_state()
    assert not profile.exists()
