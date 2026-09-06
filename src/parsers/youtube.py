from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from ..config import Settings
from .base import LinkMetadata, MediaType, ParserError
from .card_http import get_response, json_object
from .og_meta import OGMetaParser

logger = logging.getLogger(__name__)

_ISO8601_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def extract_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    parts = parsed.path.strip("/").split("/")
    value = ""
    if host == "youtu.be":
        value = parts[0]
    elif host == "youtube.com" or host.endswith(".youtube.com"):
        if parts[0] == "watch":
            value = parse_qs(parsed.query).get("v", [""])[0]
        elif len(parts) > 1 and parts[0] in {"shorts", "embed", "live"}:
            value = parts[1]
    return value if re.fullmatch(r"[A-Za-z0-9_-]{11}", value) else None


def is_youtube_url(url: str) -> bool:
    return extract_video_id(url) is not None


def _parse_duration(iso: str) -> int | None:
    m = _ISO8601_RE.match(iso)
    if not m:
        return None
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    seconds = int(m.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


class YouTubeParser:
    def __init__(
        self, client: httpx.AsyncClient, api_key: str = "", settings: Settings | None = None,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._og_parser = OGMetaParser(client, settings)

    async def parse_api(self, url: str) -> LinkMetadata:
        video_id = extract_video_id(url)
        if not video_id or not self._api_key:
            raise ParserError(url, "YouTube API requires a video id and API key")
        return await self._parse_via_api(url, video_id)

    async def parse(self, url: str) -> LinkMetadata:
        video_id = extract_video_id(url)
        if not video_id:
            raise ParserError(url, "could not extract YouTube video_id")

        if self._api_key:
            try:
                return await self._parse_via_api(url, video_id)
            except ParserError:
                logger.warning("YouTube API failed for %s, falling back to OG scrape", url)

        return await self._parse_via_og(url, video_id)

    async def _parse_via_api(self, url: str, video_id: str) -> LinkMetadata:
        api_url = "https://www.googleapis.com/youtube/v3/videos"
        resp = await get_response(
            self._client, api_url, source_url=url, label="YouTube API",
            params={
                "id": video_id, "part": "snippet,contentDetails,statistics", "key": self._api_key,
            },
        )
        data = json_object(resp, url, "YouTube API")
        items = data.get("items", [])
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            raise ParserError(url, "YouTube API returned no items")

        item = items[0]
        if item.get("id") is not None and str(item["id"]) != video_id:
            raise ParserError(url, "target_mismatch: YouTube API returned another video")
        snippet = _object(item.get("snippet"))
        details = _object(item.get("contentDetails"))
        statistics = _object(item.get("statistics"))
        thumbnails = _object(snippet.get("thumbnails"))
        covers = [
            str(image["url"])
            for size in ("maxres", "high", "medium")
            if (image := _object(thumbnails.get(size))).get("url")
        ]

        return LinkMetadata(
            source_url=url,
            title=str(snippet.get("title") or ""),
            description=str(snippet.get("description") or ""),
            cover_url=covers[0] if covers else "",
            site_name="YouTube",
            platform="youtube",
            canonical_url=f"https://www.youtube.com/watch?v={video_id}",
            media_type=MediaType.VIDEO,
            channel=snippet.get("channelTitle"),
            duration_seconds=_parse_duration(str(details.get("duration") or "")),
            view_count=_parse_int(statistics.get("viewCount")),
            like_count=_parse_int(statistics.get("likeCount")),
            comment_count=_parse_int(statistics.get("commentCount")),
            cover_candidates=covers,
            has_visual=True,
            content_verified=bool(snippet.get("title")),
        )

    async def _parse_via_og(self, url: str, video_id: str) -> LinkMetadata:
        watch_url = f"https://www.youtube.com/watch?v={video_id}"
        meta = await self._og_parser.parse(watch_url)
        meta.site_name = "YouTube"
        meta.platform = "youtube"
        meta.canonical_url = watch_url
        meta.media_type = MediaType.VIDEO
        meta.has_visual = True
        meta.source_url = url
        return meta


def _parse_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
