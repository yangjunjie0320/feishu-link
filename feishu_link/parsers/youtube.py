from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from .base import LinkMetadata, MediaType, ParserError
from .og_meta import OGMetaParser

logger = logging.getLogger(__name__)

_VIDEO_ID_RES = [
    re.compile(r"youtube\.com/watch\?.*v=([A-Za-z0-9_-]{11})"),
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),
    re.compile(r"youtube\.com/shorts/([A-Za-z0-9_-]{11})"),
    re.compile(r"youtube\.com/embed/([A-Za-z0-9_-]{11})"),
]

_ISO8601_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def extract_video_id(url: str) -> str | None:
    for pattern in _VIDEO_ID_RES:
        m = pattern.search(url)
        if m:
            return m.group(1)
    return None


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
    def __init__(self, client: httpx.AsyncClient, api_key: str = "") -> None:
        self._client = client
        self._api_key = api_key
        self._og_parser = OGMetaParser(client)

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
        try:
            resp = await self._client.get(
                api_url,
                params={
                    "id": video_id,
                    "part": "snippet,contentDetails,statistics",
                    "key": self._api_key,
                },
            )
        except httpx.RequestError as e:
            raise ParserError(url, f"YouTube API request error: {e}") from e

        if resp.status_code >= 400:
            raise ParserError(url, f"YouTube API HTTP {resp.status_code}")

        data: dict[str, Any] = resp.json()
        items = data.get("items", [])
        if not items:
            raise ParserError(url, "YouTube API returned no items")

        item = items[0]
        snippet: dict[str, Any] = item.get("snippet", {})
        details: dict[str, Any] = item.get("contentDetails", {})
        statistics: dict[str, Any] = item.get("statistics", {})

        thumbnails: dict[str, Any] = snippet.get("thumbnails", {})
        cover = (
            thumbnails.get("maxres", {}).get("url")
            or thumbnails.get("high", {}).get("url")
            or thumbnails.get("medium", {}).get("url")
            or ""
        )

        return LinkMetadata(
            source_url=url,
            title=snippet.get("title", ""),
            description=snippet.get("description", "")[:200],
            cover_url=cover,
            site_name="YouTube",
            platform="youtube",
            canonical_url=f"https://www.youtube.com/watch?v={video_id}",
            media_type=MediaType.VIDEO,
            channel=snippet.get("channelTitle"),
            duration_seconds=_parse_duration(details.get("duration", "")),
            view_count=_parse_int(statistics.get("viewCount")),
            like_count=_parse_int(statistics.get("likeCount")),
            comment_count=_parse_int(statistics.get("commentCount")),
        )

    async def _parse_via_og(self, url: str, video_id: str) -> LinkMetadata:
        watch_url = f"https://www.youtube.com/watch?v={video_id}"
        meta = await self._og_parser.parse(watch_url)
        meta.site_name = "YouTube"
        meta.platform = "youtube"
        meta.canonical_url = watch_url
        meta.media_type = MediaType.VIDEO
        meta.source_url = url
        return meta


def _parse_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value))
    except ValueError:
        return None
