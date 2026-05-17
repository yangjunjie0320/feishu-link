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


class FallbackParser:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def parse(self, url: str) -> LinkMetadata:
        domain = re.sub(r"^www\.", "", urlparse(url).netloc)
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
        title_el = soup.find("title")
        title = title_el.get_text(strip=True) if title_el else ""

        if not title and not domain:
            raise ParserError(url, "could not extract title or domain")

        return LinkMetadata(
            source_url=url,
            title=title or domain,
            site_name=domain,
            platform=detect_platform(url),
        )
