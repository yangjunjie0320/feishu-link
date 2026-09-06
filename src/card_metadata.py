from __future__ import annotations

import asyncio
import copy
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qs, urlparse

from .parsers.base import CardParseResult, CardStatus, LinkMetadata, MediaType, ParserError
from .platforms import detect_platform, normalize_url


class CardSourceError(ParserError):
    """A source failure with enough information for bounded recovery."""

    def __init__(
        self, url: str, reason: str, *, kind: str = "content", retry_after: float | None = None,
    ) -> None:
        super().__init__(url, reason)
        self.kind = kind
        self.retry_after = retry_after


def is_placeholder(text: str, platform: str, url: str = "") -> bool:
    value = " ".join(text.strip().lower().split())
    domain = (urlparse(url).hostname or "").removeprefix("www.")
    if value in {
        "", domain, platform, "x", "twitter", "instagram", "youtube", "tiktok", "douyin",
        "x.com", "twitter.com", "instagram.com", "youtube.com", "youtu.be", "tiktok.com",
        "douyin.com", "抖音", "抖音-记录美好生活", "抖音 - 记录美好生活",
        "tiktok - make your day", "make your day", "instagram post", "instagram reel",
        "x post", "x video", "youtube video", "tiktok post", "tiktok video", "douyin video",
        "video", "post", "login", "log in", "sign in", "just a moment...", "access denied",
        "something went wrong", "page not found", "content unavailable",
    }:
        return True
    if value.startswith(("post by ", "video by ")):
        return True
    if value.startswith((
        "enjoy the videos and music you love", "create an account or log in to instagram",
        "from breaking news and entertainment to sports and politics",
        "tiktok is the destination for short-form mobile videos",
    )):
        return True
    return bool(re.match(
        r"^(?:log in|login|sign in|sign up|security check|security verification|"
        r"verify you are human|verify to continue|captcha|before you continue to youtube)"
        r"(?:\b|\s|[|·•-])", value
    )) or value.startswith(("登录", "安全验证", "请完成验证", "验证后继续"))


def _clean_metadata(meta: LinkMetadata) -> None:
    for field in ("title", "description"):
        if is_placeholder(getattr(meta, field), meta.platform, meta.source_url):
            setattr(meta, field, "")
    candidates: list[str] = []
    for candidate in [meta.cover_url, *meta.cover_candidates]:
        candidate = candidate.strip()
        if candidate.startswith("//"):
            candidate = "https:" + candidate
        if urlparse(candidate).scheme in {"http", "https"} and candidate not in candidates:
            candidates.append(candidate)
    meta.cover_candidates = candidates[:3]
    meta.cover_url = candidates[0] if candidates else ""
    if candidates or meta.media_type == MediaType.VIDEO:
        meta.has_visual = True


def merge_metadata(target: LinkMetadata, source: LinkMetadata) -> None:
    """Keep real fields already obtained, filling gaps from an independent source."""
    source = copy.deepcopy(source)
    _clean_metadata(target)
    _clean_metadata(source)
    for field in ("title", "description", "channel", "site_name", "canonical_url"):
        existing = getattr(target, field)
        incoming = getattr(source, field)
        link_only = (
            field == "description" and _only_urls(existing)
            and incoming and not _only_urls(incoming)
        )
        if incoming and (not existing or link_only):
            setattr(target, field, getattr(source, field))
    for field in ("duration_seconds", "view_count", "like_count", "comment_count", "repost_count"):
        if getattr(target, field) is None and getattr(source, field) is not None:
            setattr(target, field, getattr(source, field))
    if target.platform == "web" and source.platform != "web":
        target.platform = source.platform
    verified_social_type = (
        target.platform in {"instagram", "tiktok", "douyin", "x"}
        and source.platform == target.platform
        and source.content_verified
        and not target.content_verified
        and source.media_type != MediaType.UNKNOWN
    )
    if (
        source.media_type == MediaType.VIDEO
        or target.media_type == MediaType.UNKNOWN
        or verified_social_type
    ):
        target.media_type = source.media_type
    target.cover_candidates = list(dict.fromkeys([
        *target.cover_candidates, *source.cover_candidates,
    ]))[:3]
    if not target.cover_url:
        target.cover_url = source.cover_url
    if source.has_visual is True or target.has_visual is None:
        target.has_visual = source.has_visual
    target.content_verified = target.content_verified or source.content_verified
    target.requires_auth = target.requires_auth or source.requires_auth
    _clean_metadata(target)


def card_result(
    meta: LinkMetadata, sources: list[str] | None = None, reason: str = "",
) -> CardParseResult:
    _clean_metadata(meta)
    text = bool(meta.title or meta.description)
    image = bool(meta.cover_url)
    has_content = text or (image and meta.content_verified)
    complete = has_content and (
        (meta.has_visual is False and text)
        or (image and (text or (
            meta.content_verified and meta.media_type == MediaType.ARTICLE
            and meta.has_visual is True
        )))
    )
    status = CardStatus.COMPLETE if complete else (
        CardStatus.PARTIAL if has_content else CardStatus.UNAVAILABLE
    )
    if not reason and not complete:
        reason = "partial" if has_content else "unavailable"
    return CardParseResult(
        meta, status, "" if complete else reason, list(sources or []), has_content,
    )


def _only_urls(value: str) -> bool:
    return bool(value.strip()) and all(
        token.startswith(("https://", "http://", "t.co/")) for token in value.split()
    )


def content_key(url: str) -> str:
    platform = detect_platform(url)
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    query = parse_qs(parsed.query)
    if platform == "youtube":
        video_id = query.get("v", [""])[0]
        if parsed.hostname == "youtu.be" and parts:
            video_id = parts[0]
        elif len(parts) > 1 and parts[0] in {"shorts", "embed", "live"}:
            video_id = parts[1]
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            return f"youtube:{video_id}"
    if platform == "x" and "status" in parts:
        index = parts.index("status") + 1
        if index < len(parts) and parts[index].isdigit():
            return f"x:{parts[index]}"
    if platform == "instagram" and len(parts) > 1 and parts[0] in {"p", "reel", "reels", "tv"}:
        return f"instagram:{parts[1]}:image={query.get('img_index', ['1'])[0]}"
    if platform in {"tiktok", "douyin"}:
        for kind in ("video", "photo", "note"):
            if kind in parts:
                index = parts.index(kind) + 1
                if index < len(parts) and parts[index].isdigit():
                    return f"{platform}:{parts[index]}"
        video_id = query.get("modal_id", [""])[0]
        if video_id.isdigit():
            return f"{platform}:{video_id}"
    return normalize_url(url)


class CardMetadataCache:
    """Bounded complete-result cache and cancellation-isolated shared work."""

    def __init__(self, ttl: float = 600, capacity: int = 256) -> None:
        self._ttl = ttl
        self._capacity = capacity
        self._cache: OrderedDict[str, tuple[float, CardParseResult]] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[CardParseResult]] = {}

    def invalidate(self, url: str) -> None:
        self._cache.pop(content_key(url), None)

    async def get(
        self, url: str, factory: Callable[[], Awaitable[CardParseResult]],
    ) -> CardParseResult:
        key = content_key(url)
        cached = self._cache.get(key)
        if cached is not None:
            if cached[0] > time.monotonic():
                self._cache.move_to_end(key)
                return self._copy(cached[1], url)
            del self._cache[key]
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(factory())
            self._inflight[key] = task
            task.add_done_callback(lambda done: self._finished(key, done))
        return self._copy(await asyncio.shield(task), url)

    def _finished(self, key: str, task: asyncio.Task[CardParseResult]) -> None:
        if self._inflight.get(key) is task:
            del self._inflight[key]
        if task.cancelled() or task.exception() is not None:
            return
        result = task.result()
        if result.status != CardStatus.COMPLETE or self._ttl <= 0 or self._capacity <= 0:
            return
        self._cache[key] = (time.monotonic() + self._ttl, copy.deepcopy(result))
        self._cache.move_to_end(key)
        while len(self._cache) > self._capacity:
            self._cache.popitem(last=False)

    @staticmethod
    def _copy(result: CardParseResult, url: str) -> CardParseResult:
        result = copy.deepcopy(result)
        result.metadata.source_url = url
        return result
