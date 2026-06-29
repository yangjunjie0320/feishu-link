from __future__ import annotations

from urllib.parse import urlencode, urlparse

import httpx
from bs4 import BeautifulSoup

from .base import LinkMetadata, MediaType, ParserError


class XOEmbedParser:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def parse(self, url: str) -> LinkMetadata:
        oembed_url = canonical_x_status_url(url)
        endpoint = "https://publish.twitter.com/oembed?" + urlencode({
            "omit_script": "1",
            "url": oembed_url,
        })
        try:
            resp = await self._client.get(endpoint, follow_redirects=True)
        except httpx.RequestError as e:
            raise ParserError(url, f"x oEmbed request error: {e}") from e

        if resp.status_code >= 400:
            raise ParserError(url, f"x oEmbed HTTP {resp.status_code}")

        data = resp.json()
        html = str(data.get("html") or "")
        text = _extract_tweet_text(html)
        author_name = str(data.get("author_name") or "").strip()
        author_url = str(data.get("author_url") or "").strip()
        handle = _handle_from_author_url(author_url)
        if not text and not author_name and not handle:
            raise ParserError(url, "x oEmbed returned no post content")

        channel = handle or author_name or None
        return LinkMetadata(
            source_url=url,
            title=f"Post by {channel}" if channel else "X Post",
            description=text,
            site_name="X",
            platform="x",
            canonical_url=str(data.get("url") or url),
            media_type=MediaType.ARTICLE,
            channel=channel,
        )


def _extract_tweet_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    paragraph = soup.find("p")
    if paragraph is None:
        return ""
    return " ".join(paragraph.get_text(" ", strip=True).split())


def _handle_from_author_url(author_url: str) -> str:
    path = urlparse(author_url).path.strip("/")
    if not path:
        return ""
    handle = path.split("/", 1)[0]
    return f"@{handle}" if handle else ""


def canonical_x_status_url(url: str) -> str:
    """Strip media subpaths like /status/{id}/video/1 for public tweet endpoints."""
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if "status" not in parts:
        return url

    index = parts.index("status")
    if index + 1 >= len(parts):
        return url

    canonical_parts = parts[: index + 2]
    return parsed._replace(
        path="/" + "/".join(canonical_parts),
        params="",
        fragment="",
    ).geturl()
