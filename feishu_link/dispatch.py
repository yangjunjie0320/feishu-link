from __future__ import annotations

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
                meta.media_type = MediaType.VIDEO
                meta.parse_warnings.append(_friendly_parse_warning(meta.platform, e.reason))
                if _is_generic_title(meta.title, meta.platform):
                    meta.title = _fallback_video_title(meta.platform)
                return meta
            return await self._fallback.parse(url)

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


def _friendly_parse_warning(platform: str, reason: str) -> str:
    lowered = reason.lower()
    if "login required" in lowered or "rate-limit" in lowered or "not available" in lowered:
        return f"{platform} 内容受限或需要 cookie, 已先发送卡片"
    return f"{platform} 视频解析失败, 已先发送卡片"


def _is_generic_title(title: str, platform: str) -> bool:
    normalized = title.strip().lower()
    return normalized in {"", platform, "instagram", "x", "twitter", "tiktok"}


def _fallback_video_title(platform: str) -> str:
    labels = {
        "instagram": "Instagram Reel",
        "tiktok": "TikTok Video",
        "x": "X Video",
        "bilibili": "Bilibili Video",
        "youtube": "YouTube Video",
    }
    return labels.get(platform, "Video")
