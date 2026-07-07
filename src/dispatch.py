from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import httpx

from .config import Settings
from .parsers.base import LinkMetadata, MediaType, ParserError
from .parsers.fallback import FallbackParser
from .parsers.instagram_media_info import InstagramMediaInfoParser
from .parsers.og_meta import OGMetaParser
from .parsers.x_graphql import XGraphQLParser
from .parsers.x_oembed import XOEmbedParser
from .parsers.youtube import YouTubeParser, is_youtube_url
from .parsers.ytdlp import YtDlpMetadataParser
from .platforms import detect_platform

logger = logging.getLogger(__name__)


class Dispatcher:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._youtube = YouTubeParser(client, api_key=settings.youtube_api_key)
        self._ytdlp = YtDlpMetadataParser(settings)
        self._instagram_media_info = InstagramMediaInfoParser(client, settings)
        self._x_graphql = XGraphQLParser(client, settings)
        self._x_oembed = XOEmbedParser(client)
        self._og = OGMetaParser(client, settings)
        self._fallback = FallbackParser(client, settings)

    async def parse(self, url: str) -> LinkMetadata:
        if is_youtube_url(url):
            return await self._parse_youtube(url)
        if _is_instagram_post_url(url):
            return await self._parse_instagram_post(url)

        try:
            meta = await self._ytdlp.parse(url)
            _normalize_short_platform_meta(meta)
            return meta
        except ParserError as e:
            logger.warning("ytdlp metadata failed: %s", e)
            if detect_platform(url) == "x":
                try:
                    meta = await self._x_oembed.parse(url)
                except ParserError:
                    meta = await self._parse_og_or_fallback(url)
                if not meta.cover_url:
                    try:
                        graphql_meta = await self._x_graphql.parse(url)
                    except ParserError:
                        graphql_meta = None
                    if graphql_meta:
                        _merge_missing_social_fields(meta, graphql_meta)
            else:
                meta = await self._parse_og_or_fallback(url)
            meta.platform = meta.platform or "web"
            meta.media_type = _fallback_media_type(url, meta.platform, e.reason)
            if meta.media_type == MediaType.ARTICLE:
                _normalize_image_post_meta(meta)
            warning = _friendly_parse_warning(url, meta.platform, e.reason)
            if warning:
                meta.parse_warnings.append(warning)
            if _is_generic_title(meta.title, meta.platform):
                meta.title = _fallback_title(url, meta.platform, meta.media_type)
            return meta

    async def _parse_og_or_fallback(self, url: str) -> LinkMetadata:
        try:
            return await self._og.parse(url)
        except ParserError:
            return await self._fallback.parse(url)

    async def _parse_instagram_post(self, url: str) -> LinkMetadata:
        try:
            meta = await self._instagram_media_info.parse(url)
        except ParserError as e:
            logger.info("instagram media info failed for %s: %s", url, e.reason)
            try:
                meta = await self._og.parse(url)
            except ParserError:
                meta = await self._fallback.parse(url)
        if not meta.cover_url:
            try:
                og_meta = await self._og.parse(url)
            except ParserError:
                og_meta = None
            if og_meta and og_meta.cover_url:
                meta.cover_url = og_meta.cover_url
        meta.platform = "instagram"
        meta.media_type = MediaType.ARTICLE
        _normalize_image_post_meta(meta)
        meta.parse_warnings = []
        return meta

    async def _parse_youtube(self, url: str) -> LinkMetadata:
        try:
            meta = await self._youtube.parse(url)
        except ParserError:
            meta = await self._fallback.parse(url)

        try:
            logger.info("starting ytdlp metadata extraction for %s", url)
            media_meta = await self._ytdlp.parse(url)
            logger.info("finished ytdlp metadata extraction for %s", url)
        except ParserError as e:
            logger.warning("ytdlp metadata failed: %s", e)
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
        return ""
    if _is_x_post_without_video(platform, lowered):
        return ""
    if _is_social_post_without_video(platform, lowered):
        return ""
    if "login required" in lowered or "rate-limit" in lowered or "not available" in lowered:
        return f"{platform} 内容受限或需要 cookie, 已先发送卡片"
    return f"{platform} 视频解析失败, 已先发送卡片"


def _is_generic_title(title: str, platform: str) -> bool:
    normalized = title.strip().lower()
    return normalized in {"", platform, "instagram", "x", "twitter", "tiktok"}


def _fallback_media_type(url: str, platform: str, reason: str) -> MediaType:
    lowered_reason = reason.lower()
    if _is_instagram_post_without_video(url, platform, lowered_reason):
        return MediaType.ARTICLE
    if _is_x_post_without_video(platform, lowered_reason):
        return MediaType.ARTICLE
    if _is_social_post_without_video(platform, lowered_reason):
        return MediaType.ARTICLE
    return MediaType.VIDEO


def _fallback_title(url: str, platform: str, media_type: MediaType) -> str:
    if platform == "instagram" and media_type == MediaType.ARTICLE:
        if _instagram_path_kind(url) == "p":
            return "Instagram Post"
        return "Instagram"
    if platform == "x" and media_type == MediaType.ARTICLE:
        return "X Post"
    if platform == "tiktok" and media_type == MediaType.ARTICLE:
        return "TikTok Post"

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


def _is_x_post_without_video(platform: str, lowered_reason: str) -> bool:
    return platform == "x" and (
        "no video formats found" in lowered_reason
        or "unsupported url" in lowered_reason
        or "no formats" in lowered_reason
    )


def _is_social_post_without_video(platform: str, lowered_reason: str) -> bool:
    return platform in {"tiktok"} and (
        "no video formats found" in lowered_reason
        or "unsupported url" in lowered_reason
        or "no formats" in lowered_reason
    )


def _instagram_path_kind(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    return parts[0].lower() if parts else ""


def _is_instagram_post_url(url: str) -> bool:
    if _instagram_path_kind(url) != "p":
        return False
    return detect_platform(url) == "instagram"


def _normalize_image_post_meta(meta: LinkMetadata) -> None:
    if meta.platform == "instagram":
        _normalize_instagram_post_meta(meta)
        return
    if meta.platform == "x":
        _normalize_x_post_meta(meta)
        return
    if meta.platform in {"tiktok"}:
        _normalize_generic_social_post_meta(meta)


def _merge_missing_instagram_fields(meta: LinkMetadata, fallback: LinkMetadata) -> None:
    _merge_missing_social_fields(meta, fallback)


def _merge_missing_social_fields(meta: LinkMetadata, fallback: LinkMetadata) -> None:
    if not meta.title:
        meta.title = fallback.title
    if not meta.description:
        meta.description = fallback.description
    if not meta.cover_url:
        meta.cover_url = fallback.cover_url
    if not meta.channel:
        meta.channel = fallback.channel
    if meta.view_count is None:
        meta.view_count = fallback.view_count
    if meta.like_count is None:
        meta.like_count = fallback.like_count
    if meta.comment_count is None:
        meta.comment_count = fallback.comment_count
    if meta.repost_count is None:
        meta.repost_count = fallback.repost_count


def _normalize_short_platform_meta(meta: LinkMetadata) -> None:
    if meta.platform == "x" and meta.media_type != MediaType.VIDEO:
        meta.media_type = MediaType.ARTICLE
        _normalize_x_post_meta(meta)
        meta.parse_warnings = []
    if meta.platform in {"tiktok"} and meta.media_type != MediaType.VIDEO:
        meta.media_type = MediaType.ARTICLE
        _normalize_generic_social_post_meta(meta)
        meta.parse_warnings = []


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


def _normalize_instagram_post_meta(meta: LinkMetadata) -> None:
    meta.site_name = "Instagram"
    meta.channel = meta.channel or _extract_instagram_handle(meta.description)
    likes, comments = _extract_instagram_counts(meta.description)

    caption = _extract_instagram_caption(meta.description) or _extract_instagram_caption(meta.title)
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


def _normalize_x_post_meta(meta: LinkMetadata) -> None:
    meta.site_name = "X"
    meta.channel = _normalize_x_channel(meta.channel)
    description = _clean_caption(meta.description)

    if description and _is_x_placeholder_text(description):
        meta.description = ""
    elif description:
        meta.description = description
    elif meta.title and not _is_generic_title(meta.title, meta.platform):
        cleaned_title = _clean_x_title(meta.title)
        if not _is_x_placeholder_text(cleaned_title):
            meta.description = cleaned_title

    if (
        not meta.title
        or _is_generic_title(meta.title, meta.platform)
        or _is_x_placeholder_text(meta.title)
        or meta.description
    ):
        if meta.channel:
            meta.title = f"Post by {meta.channel}"
        else:
            meta.title = "X Post"

    if _x_meta_has_only_placeholder(meta):
        warning = "X 内容受限或需要 cookie, 无法获取正文, 已先发送卡片"
        if warning not in meta.parse_warnings:
            meta.parse_warnings.append(warning)


def _normalize_x_channel(channel: str | None) -> str | None:
    if not channel:
        return None
    normalized = channel.strip()
    return normalized if normalized.startswith("@") else f"@{normalized}"


def _clean_x_title(title: str) -> str:
    cleaned = re.sub(r"\s*/\s*X\s*$", "", title.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"^X\s+on\s+X:\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^[^:]{1,80}\s+on\s+X:\s*", "", cleaned, flags=re.IGNORECASE)
    return _clean_caption(cleaned.strip('"'))


def _is_x_placeholder_text(text: str) -> bool:
    normalized = " ".join(text.strip().lower().split())
    return normalized in {"", "x", "x.com", "twitter", "twitter.com"}


def _x_meta_has_only_placeholder(meta: LinkMetadata) -> bool:
    if meta.platform != "x":
        return False
    if meta.description.strip():
        return False
    if meta.channel:
        return False
    if meta.cover_url:
        return False
    counts = (
        meta.view_count,
        meta.like_count,
        meta.comment_count,
        meta.repost_count,
    )
    return all(value is None for value in counts)


def _normalize_generic_social_post_meta(meta: LinkMetadata) -> None:
    meta.channel = meta.channel or None
    if meta.description:
        meta.description = _clean_caption(meta.description)
    if not meta.title or _is_generic_title(meta.title, meta.platform):
        meta.title = _generic_social_post_title(meta.platform)


def _generic_social_post_title(platform: str) -> str:
    labels = {
        "tiktok": "TikTok Post",
    }
    return labels.get(platform, "Post")
