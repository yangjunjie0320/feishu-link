from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx

from src.comment_analyzer import comments_from_raw
from src.tiktok_comments import (
    TikTokCommentError,
    aweme_id_from_url,
    build_comment_list_url,
    normalize_tiktok_comment,
    paginate_comments,
    resolve_tiktok_url,
)

_FAR_DEADLINE = float("inf")


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


def _fetcher(pages: list[dict[str, Any]]) -> tuple[Callable[[str], Any], list[str]]:
    """Return a fetcher serving `pages` in order, plus the list of URLs it saw."""
    requested: list[str] = []

    async def fetch_json(url: str) -> Any:
        requested.append(url)
        return pages[min(len(requested) - 1, len(pages) - 1)]

    return fetch_json, requested


async def _paginate(fetch_json: Callable[[str], Any], **overrides: Any) -> tuple[Any, Any]:
    kwargs: dict[str, Any] = {
        "max_comments": 500,
        "page_size": 20,
        "max_pages": 30,
        "request_delay": 0,
        "deadline": _FAR_DEADLINE,
    }
    kwargs.update(overrides)
    return await paginate_comments(fetch_json, "123", **kwargs)


def test_aweme_id_from_url_handles_video_photo_and_query() -> None:
    assert aweme_id_from_url("https://www.tiktok.com/@a/video/7653231150139182367") == (
        "7653231150139182367"
    )
    assert aweme_id_from_url("https://www.tiktok.com/@a/photo/123?is_from_webapp=1") == "123"
    assert aweme_id_from_url("https://www.tiktok.com/@someone") == ""


def test_build_comment_list_url_carries_required_client_params() -> None:
    url = build_comment_list_url("123", cursor=40, count=20)
    assert url.startswith("https://www.tiktok.com/api/comment/list/?")
    for expected in ("aweme_id=123", "cursor=40", "count=20", "aid=1988", "app_name=tiktok_web"):
        assert expected in url


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


async def test_paginate_advances_cursor_until_has_more_is_zero() -> None:
    fetch_json, requested = _fetcher(
        [
            _page([_comment("1"), _comment("2")], cursor=20),
            _page([_comment("3")], cursor=40, has_more=0),
        ]
    )

    collected, total = await _paginate(fetch_json)

    assert len(collected) == 3
    assert total == 100
    assert "cursor=0" in requested[0]
    assert "cursor=20" in requested[1]


async def test_paginate_stops_when_cursor_does_not_advance() -> None:
    """A server that keeps echoing the same cursor must not spin forever."""
    fetch_json, requested = _fetcher([_page([_comment("1")], cursor=0)])

    collected, _ = await _paginate(fetch_json)

    assert len(requested) == 1
    assert len(collected) == 1


async def test_paginate_dedupes_pinned_comment_repeated_across_pages() -> None:
    pinned = _comment("pin")
    fetch_json, _ = _fetcher(
        [
            _page([pinned, _comment("1")], cursor=20),
            _page([pinned, _comment("2")], cursor=40, has_more=0),
        ]
    )

    collected, _ = await _paginate(fetch_json)

    assert len(collected) == 3


async def test_paginate_respects_max_comments() -> None:
    fetch_json, _ = _fetcher(
        [_page([_comment(str(i)) for i in range(20)], cursor=20 * (n + 1)) for n in range(5)]
    )

    collected, _ = await _paginate(fetch_json, max_comments=5)

    assert len(collected) == 5


async def test_paginate_respects_max_pages() -> None:
    def page_at(index: int) -> dict[str, Any]:
        return _page([_comment(f"{index}-{i}") for i in range(3)], cursor=20 * (index + 1))

    requested: list[str] = []

    async def fetch_json(url: str) -> Any:
        requested.append(url)
        return page_at(len(requested) - 1)

    await _paginate(fetch_json, max_pages=3)

    assert len(requested) == 3


async def test_paginate_returns_partial_result_on_deadline() -> None:
    def page_at(index: int) -> dict[str, Any]:
        return _page([_comment(f"{index}-{i}") for i in range(2)], cursor=20 * (index + 1))

    requested: list[str] = []

    async def fetch_json(url: str) -> Any:
        requested.append(url)
        return page_at(len(requested) - 1)

    # Already past the deadline: the first page is kept, the walk stops there.
    collected, _ = await _paginate(fetch_json, deadline=time.monotonic() - 1)

    assert len(requested) == 1
    assert len(collected) == 2


async def test_paginate_raises_login_required_on_first_page() -> None:
    fetch_json, _ = _fetcher([_page([], cursor=0, status_code=2154)])

    with pytest.raises(TikTokCommentError, match="要求登录"):
        await _paginate(fetch_json)


async def test_paginate_raises_item_unavailable_on_first_page() -> None:
    fetch_json, _ = _fetcher([_page([], cursor=0, status_code=2053)])

    with pytest.raises(TikTokCommentError, match="不存在或已被删除"):
        await _paginate(fetch_json)


async def test_paginate_reports_unknown_status_code() -> None:
    fetch_json, _ = _fetcher([_page([], cursor=0, status_code=4001)])

    with pytest.raises(TikTokCommentError, match="status_code=4001"):
        await _paginate(fetch_json)


async def test_paginate_keeps_partial_result_when_later_page_errors() -> None:
    fetch_json, _ = _fetcher(
        [
            _page([_comment("1")], cursor=20),
            _page([], cursor=40, status_code=2154),
        ]
    )

    collected, _ = await _paginate(fetch_json)

    assert len(collected) == 1


async def test_paginate_raises_when_video_has_no_comments() -> None:
    fetch_json, _ = _fetcher([_page([], cursor=0, has_more=0, total=0)])

    with pytest.raises(TikTokCommentError, match="没有评论或评论区已关闭"):
        await _paginate(fetch_json)


async def test_paginate_rejects_non_dict_payload() -> None:
    async def fetch_json(url: str) -> Any:
        return "<html>captcha</html>"

    with pytest.raises(TikTokCommentError, match="无法解析"):
        await _paginate(fetch_json)


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
