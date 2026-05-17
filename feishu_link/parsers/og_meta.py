from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from ..platforms import detect_platform
from .base import LinkMetadata, ParserError

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class OGMetaParser:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def parse(self, url: str) -> LinkMetadata:
        try:
            resp = await self._client.get(
                url,
                headers={"User-Agent": _USER_AGENT},
                follow_redirects=True,
            )
        except httpx.RequestError as e:
            raise ParserError(url, f"request error: {e}") from e

        if resp.status_code >= 400:
            raise ParserError(url, f"HTTP {resp.status_code}")

        soup = BeautifulSoup(resp.text, "lxml")
        meta = _extract_og(soup)

        domain = re.sub(r"^www\.", "", urlparse(url).netloc)
        return LinkMetadata(
            source_url=url,
            title=meta.get("og:title") or _tag_text(soup, "title") or domain,
            description=meta.get("og:description", ""),
            cover_url=meta.get("og:image", ""),
            site_name=meta.get("og:site_name") or domain,
            platform=detect_platform(url),
        )


def _extract_og(soup: BeautifulSoup) -> dict[str, str]:
    result: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        prop = tag.get("property", "") or tag.get("name", "")
        content = tag.get("content", "")
        if prop and content:
            result[prop] = content
    return result


def _tag_text(soup: BeautifulSoup, tag: str) -> str:
    el = soup.find(tag)
    return el.get_text(strip=True) if el else ""
