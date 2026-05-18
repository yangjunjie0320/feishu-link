from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import httpx

from .config import Settings
from .parsers.base import LinkMetadata, MediaType, Parser, ParserError
from .parsers.fallback import FallbackParser
from .parsers.og_meta import OGMetaParser
from .parsers.youtube import YouTubeParser, is_youtube_url
from .parsers.ytdlp import YtDlpMetadataParser
from .platforms import is_short_video_platform


class Dispatcher:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._youtube = YouTubeParser(client, api_key=settings.youtube_api_key)
        self._ytdlp = YtDlpMetadataParser(settings)
        self._og = OGMetaParser(client)
        self._fallback = FallbackParser(client)

    async def parse(self, url: str) -> LinkMetadata:
        if is_youtube_url(url):
            return await self._parse_youtube(url)
        if _is_instagram_image_post_url(url):
            return await self._parse_instagram_image_post(url)

        parser: Parser
        parser = self._ytdlp if is_short_video_platform(url) else self._og

        try:
            return await parser.parse(url)
        except ParserError as e:
            if parser is self._ytdlp:
                try:
                    meta = await self._og.parse(url)
                except ParserError:
                    meta = await self._fallback.parse(url)
                meta.platform = meta.platform or "web"
                meta.media_type = _fallback_media_type(url, meta.platform, e.reason)
                if meta.media_type == MediaType.ARTICLE and meta.platform == "instagram":
                    _normalize_instagram_post_meta(meta)
                meta.parse_warnings.append(
                    _friendly_parse_warning(url, meta.platform, e.reason)
                )
                if _is_generic_title(meta.title, meta.platform):
                    meta.title = _fallback_title(url, meta.platform, meta.media_type)
                return meta
            return await self._fallback.parse(url)

    async def _parse_instagram_image_post(self, url: str) -> LinkMetadata:
        try:
            meta = await self._ytdlp.parse(url)
        except ParserError:
            try:
                meta = await self._og.parse(url)
            except ParserError:
                meta = await self._fallback.parse(url)
        meta.platform = "instagram"
        meta.media_type = MediaType.ARTICLE
        _normalize_instagram_post_meta(meta)
        meta.parse_warnings = ["instagram 图文内容已发送卡片, 未尝试下载视频"]
        return meta

    async def _parse_youtube(self, url: str) -> LinkMetadata:
        try:
            meta = await self._youtube.parse(url)
        except ParserError:
            meta = await self._fallback.parse(url)

        try:
            media_meta = await self._ytdlp.parse(url)
        except ParserError as e:
            meta.parse_warnings.append(e.reason)
            return meta

        meta.download_candidates = media_meta.download_candidates
        meta.requires_auth = media_meta.requires_auth
        meta.media_type = media_meta.media_type
        if not meta.canonical_url:
            meta.canonical_url = media_meta.canonical_url
        if meta.duration_seconds is None:
            meta.duration_seconds = media_meta.duration_seconds
        if meta.view_count is None:
            meta.view_count = media_meta.view_count
        if meta.like_count is None:
            meta.like_count = media_meta.like_count
        if meta.comment_count is None:
            meta.comment_count = media_meta.comment_count
        if meta.repost_count is None:
            meta.repost_count = media_meta.repost_count
        meta.parse_warnings.extend(media_meta.parse_warnings)
        return meta


def _friendly_parse_warning(url: str, platform: str, reason: str) -> str:
    lowered = reason.lower()
    if _is_instagram_post_without_video(url, platform, lowered):
        return "instagram 图文内容已发送卡片, 未发现可下载视频"
    if "login required" in lowered or "rate-limit" in lowered or "not available" in lowered:
        return f"{platform} 内容受限或需要 cookie, 已先发送卡片"
    return f"{platform} 视频解析失败, 已先发送卡片"


def _is_generic_title(title: str, platform: str) -> bool:
    normalized = title.strip().lower()
    return normalized in {"", platform, "instagram", "x", "twitter", "tiktok"}


def _fallback_media_type(url: str, platform: str, reason: str) -> MediaType:
    if _is_instagram_post_without_video(url, platform, reason.lower()):
        return MediaType.ARTICLE
    return MediaType.VIDEO


def _fallback_title(url: str, platform: str, media_type: MediaType) -> str:
    if platform == "instagram" and media_type == MediaType.ARTICLE:
        if _instagram_path_kind(url) == "p":
            return "Instagram Post"
        return "Instagram"

    labels = {
        "instagram": "Instagram Reel",
        "tiktok": "TikTok Video",
        "x": "X Video",
        "bilibili": "Bilibili Video",
        "youtube": "YouTube Video",
    }
    return labels.get(platform, "Video")


def _is_instagram_post_without_video(url: str, platform: str, lowered_reason: str) -> bool:
    return (
        platform == "instagram"
        and _instagram_path_kind(url) == "p"
        and "no video formats found" in lowered_reason
    )


def _instagram_path_kind(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    return parts[0].lower() if parts else ""


def _is_instagram_image_post_url(url: str) -> bool:
    parsed = urlparse(url)
    if _instagram_path_kind(url) != "p":
        return False
    if "instagram.com" not in parsed.netloc.lower():
        return False
    return "img_index" in parse_qs(parsed.query)


def _normalize_instagram_post_meta(meta: LinkMetadata) -> None:
    meta.site_name = "Instagram"
    meta.channel = meta.channel or _extract_instagram_handle(meta.description)
    likes, comments = _extract_instagram_counts(meta.description)

    caption = _extract_instagram_caption(meta.description) or _extract_instagram_caption(
        meta.title
    )
    if caption:
        meta.description = caption

    if not meta.title or _is_generic_title(meta.title, meta.platform) or caption:
        if meta.channel:
            meta.title = f"Post by {meta.channel}"
        else:
            meta.title = "Instagram Post"

    if meta.like_count is None:
        meta.like_count = likes
    if meta.comment_count is None:
        meta.comment_count = comments


def _extract_instagram_caption(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""

    quoted = re.search(r':\s*"(?P<caption>.+?)"\.?\s*$', stripped, flags=re.DOTALL)
    if quoted:
        return _clean_caption(quoted.group("caption"))

    title_match = re.search(
        r"\bon instagram:\s*\"(?P<caption>.+?)\"\s*$",
        stripped,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if title_match:
        return _clean_caption(title_match.group("caption"))

    return ""


def _extract_instagram_handle(description: str) -> str | None:
    match = re.search(r"-\s*([A-Za-z0-9._]+)\s+on\s+", description)
    return match.group(1) if match else None


def _extract_instagram_counts(description: str) -> tuple[int | None, int | None]:
    likes_match = re.search(r"([\d,.]+)\s*([KMB])?\s+likes?", description, re.I)
    comments_match = re.search(r"([\d,.]+)\s*([KMB])?\s+comments?", description, re.I)
    return _parse_compact_count(likes_match), _parse_compact_count(comments_match)


def _parse_compact_count(match: re.Match[str] | None) -> int | None:
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    suffix = (match.group(2) or "").upper()
    multiplier = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(suffix, 1)
    return int(number * multiplier)


def _clean_caption(text: str) -> str:
    return " ".join(text.strip().split())
