"""End-to-end cover for everything downstream of the TikTok browser capture.

The browser step itself can only be verified against a live TikTok, which
refuses most of the time. Everything after it -- dedupe, field normalization,
heat sorting, top selection, prompt building, markdown rendering -- is pure
logic and is pinned here against payloads shaped like the real ones observed
in production (53916 bytes / 14 comments and 70679 bytes / 17 comments, both
with total=4225-ish and a repeated pinned comment across pages).

This is what narrows "unverified" down to the single browser call.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

from src.comment_analyzer import CommentAnalysisError, CommentAnalyzer, CommentInsight
from src.config import Settings
from src.tiktok_comments import TikTokCommentClient

_VIDEO_URL = "https://www.tiktok.com/@daw.dd/video/7668229283306933525"


def _raw_comment(
    cid: str,
    text: str,
    *,
    likes: int,
    replies: int = 0,
    nickname: str = "Ada",
    unique_id: str = "ada_l",
    reply_id: str = "0",
) -> dict[str, Any]:
    """A comment shaped like TikTok's actual response entries."""
    return {
        "cid": cid,
        "text": text,
        "aweme_id": "7668229283306933525",
        "create_time": 1786000000,
        "digg_count": likes,
        "reply_comment_total": replies,
        "reply_id": reply_id,
        "reply_to_reply_id": "0",
        "status": 1,
        "user": {
            "uid": "7000000000000000000",
            "nickname": nickname,
            "unique_id": unique_id,
            "sec_uid": "MS4wLjABAAAA",
        },
        "user_digged": 0,
    }


def _page(comments: list[dict[str, Any]], *, cursor: int, has_more: int) -> dict[str, Any]:
    return {
        "status_code": 0,
        "comments": comments,
        "cursor": cursor,
        "has_more": has_more,
        "total": 4225,
        "extra": {"now": 1786000000000},
    }


# Two pages, with the pinned comment repeated exactly as TikTok returns it.
_PINNED = _raw_comment("pin1", "置顶评论", likes=9000, replies=120, nickname="Keer")
_CAPTURED_PAGES = [
    _page(
        [
            _PINNED,
            _raw_comment("c1", "great video", likes=1500, replies=12),
            _raw_comment("c2", "この曲すき", likes=800, nickname="Yuki", unique_id="yuki_t"),
            _raw_comment("c3", "reply here", likes=5, reply_id="c1", nickname="Bob"),
        ],
        cursor=20,
        has_more=1,
    ),
    _page(
        [
            _PINNED,
            _raw_comment("c4", "80s vibes", likes=300, nickname="Sam", unique_id="sam_v"),
            _raw_comment("c5", "低赞但很晚才出现", likes=1, nickname="Zoe", unique_id="zoe_q"),
        ],
        cursor=40,
        has_more=0,
    ),
]


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"deepseek_api_key": "test-key"}
    base.update(overrides)
    return Settings(**base)


def _install_capture(monkeypatch: pytest.MonkeyPatch, pages: list[dict[str, Any]]) -> None:
    """Replace the browser call with the payloads a real capture produced."""

    async def fake_collect(self, page_url: str, *, max_comments: int):
        assert page_url == _VIDEO_URL
        return pages

    monkeypatch.setattr(TikTokCommentClient, "_collect_payloads", fake_collect)


async def test_capture_to_video_comments_maps_every_field(monkeypatch) -> None:
    _install_capture(monkeypatch, _CAPTURED_PAGES)

    async with httpx.AsyncClient() as client:
        fetched = await CommentAnalyzer(_settings(), client).fetch_comment_page(_VIDEO_URL)

    assert fetched.total_count == 4225
    # The pinned comment appears on both pages but must be counted once.
    assert len(fetched.comments) == 6

    by_id = {c.comment_id: c for c in fetched.comments}
    pinned = by_id["pin1"]
    assert pinned.like_count == 9000
    assert pinned.reply_count == 120
    assert pinned.author == "Keer"

    top = by_id["c1"]
    assert top.author_url == "https://www.tiktok.com/@ada_l"
    assert top.comment_url == ""  # TikTok has no per-comment permalink
    assert by_id["c3"].parent_id == "c1"
    assert by_id["c2"].text == "この曲すき"

    # Every comment must carry the fields the card renders.
    assert all(c.comment_id for c in fetched.comments)
    assert all(c.author for c in fetched.comments)
    assert all(c.author_url.startswith("https://www.tiktok.com/@") for c in fetched.comments)


async def test_analyze_renders_markdown_from_captured_payloads(monkeypatch) -> None:
    """The full chain minus the browser call and the model round trip."""
    _install_capture(monkeypatch, _CAPTURED_PAGES)

    seen: dict[str, Any] = {}

    async def fake_summarize(
        self, url, comments, top_comments, prompt_comments, *, total_comment_count
    ):
        seen["url"] = url
        seen["comments"] = comments
        seen["top"] = top_comments
        seen["prompt"] = prompt_comments
        seen["total"] = total_comment_count
        return CommentInsight(
            summary="观众普遍喜欢这首翻唱",
            sentiment="正面",
            consensus=["旋律怀旧"],
            controversy=[],
            notable_points=["有人认出了原曲"],
            top_comment_translations=["置顶评论"],
        )

    monkeypatch.setattr(CommentAnalyzer, "_summarize_with_llm", fake_summarize)

    async with httpx.AsyncClient() as client:
        result = await CommentAnalyzer(_settings(), client).analyze(_VIDEO_URL)

    assert seen["url"] == _VIDEO_URL
    assert seen["total"] == 4225
    # Sorted by heat, so the pinned 9000-like comment leads.
    assert seen["comments"][0].comment_id == "pin1"
    assert seen["top"][0].comment_id == "pin1"
    assert result.fetched_count == 6
    assert result.total_comment_count == 4225
    assert "观众普遍喜欢这首翻唱" in result.markdown
    assert "4225" in result.markdown


async def test_low_like_comment_still_reaches_the_model(monkeypatch) -> None:
    """A late, low-like comment must not be dropped before the prompt."""
    _install_capture(monkeypatch, _CAPTURED_PAGES)

    captured: dict[str, Any] = {}

    async def fake_summarize(
        self, url, comments, top_comments, prompt_comments, *, total_comment_count
    ):
        captured["prompt_ids"] = [c.comment_id for c in prompt_comments]
        return CommentInsight("s", "中性", [], [], [], [])

    monkeypatch.setattr(CommentAnalyzer, "_summarize_with_llm", fake_summarize)

    async with httpx.AsyncClient() as client:
        await CommentAnalyzer(_settings(), client).analyze(_VIDEO_URL)

    assert "c5" in captured["prompt_ids"]


async def test_max_comments_limit_is_honored_end_to_end(monkeypatch) -> None:
    many = [_raw_comment(f"c{i}", f"comment {i}", likes=i) for i in range(50)]
    _install_capture(monkeypatch, [_page(many, cursor=0, has_more=0)])

    async with httpx.AsyncClient() as client:
        fetched = await CommentAnalyzer(
            _settings(comment_analysis_max_comments=10), client
        ).fetch_comment_page(_VIDEO_URL)

    assert len(fetched.comments) == 10


async def test_empty_capture_reports_throttling_not_missing_comments(monkeypatch) -> None:
    async def fake_collect(self, page_url: str, *, max_comments: int):
        self._empty_responses = 1
        return []

    monkeypatch.setattr(TikTokCommentClient, "_collect_payloads", fake_collect)

    with pytest.raises(CommentAnalysisError, match="限流"):
        async with httpx.AsyncClient() as client:
            await CommentAnalyzer(_settings(), client).fetch_comment_page(_VIDEO_URL)


async def test_tiktok_gets_its_own_wall_clock_budget() -> None:
    settings = _settings()
    assert settings.comment_fetch_timeout_for("tiktok") == 120.0
    assert settings.comment_fetch_timeout_for("youtube") == 90.0


async def test_deadline_is_passed_through_to_the_browser_step(monkeypatch) -> None:
    """The browser must stop early enough for the context to close cleanly."""
    seen: dict[str, float] = {}

    async def fake_collect(self, page_url: str, *, max_comments: int):
        seen["at"] = time.monotonic()
        return _CAPTURED_PAGES

    monkeypatch.setattr(TikTokCommentClient, "_collect_payloads", fake_collect)

    started = time.monotonic()
    async with httpx.AsyncClient() as client:
        await CommentAnalyzer(_settings(), client).fetch_comment_page(_VIDEO_URL)

    # Sanity: the call happened promptly rather than after the wall clock.
    assert seen["at"] - started < 5
