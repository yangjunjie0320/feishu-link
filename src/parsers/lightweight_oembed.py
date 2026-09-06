from __future__ import annotations

from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from ..card_metadata import content_key, is_placeholder
from ..platforms import detect_platform
from .base import LinkMetadata, MediaType, ParserError
from .card_http import get_response, json_object
from .youtube import extract_video_id


class LightweightOEmbedParser:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def parse(self, url: str) -> LinkMetadata:
        platform = detect_platform(url)
        if platform == "youtube" and (video_id := extract_video_id(url)):
            canonical = f"https://www.youtube.com/watch?v={video_id}"
            endpoint = "https://www.youtube.com/oembed"
        elif platform == "tiktok" and "/video/" in urlparse(url).path:
            canonical = url
            endpoint = "https://www.tiktok.com/oembed"
        else:
            raise ParserError(url, "oEmbed requires a supported video URL")
        response = await get_response(
            self._client, endpoint, source_url=url, label=f"{platform} oEmbed",
            params={"url": canonical, "format": "json"},
        )
        data = json_object(response, url, f"{platform} oEmbed")
        title = str(data.get("title") or "").strip()
        cover = str(data.get("thumbnail_url") or "")
        if is_placeholder(title, platform, url):
            title = ""
        html = BeautifulSoup(str(data.get("html") or ""), "lxml")
        if platform == "tiktok":
            embed = html.find("blockquote")
            embedded_url = str(embed.get("cite") or "") if embed else ""
            if embedded_url and content_key(embedded_url) != content_key(url):
                raise ParserError(url, "TikTok oEmbed returned another video")
            if embedded_url:
                canonical = embedded_url
        if not title and not cover:
            raise ParserError(url, f"{platform} oEmbed returned no content")
        return LinkMetadata(
            source_url=url, title=title, description=title if platform == "tiktok" else "",
            cover_url=cover, cover_candidates=[cover] if cover else [],
            site_name="YouTube" if platform == "youtube" else "TikTok", platform=platform,
            canonical_url=canonical, media_type=MediaType.VIDEO,
            channel=str(data.get("author_name") or "") or None,
            has_visual=True, content_verified=True,
        )
