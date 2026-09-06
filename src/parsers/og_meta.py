from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from ..card_metadata import content_key, is_placeholder, merge_metadata
from ..config import Settings
from ..cookie_utils import get_cookie_header
from ..platforms import detect_platform
from .base import LinkMetadata, MediaType, ParserError
from .card_http import get_response

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class OGMetaParser:
    def __init__(self, client: httpx.AsyncClient, settings: Settings | None = None) -> None:
        self._client = client
        self._settings = settings

    async def parse(self, url: str) -> LinkMetadata:
        return await self._parse(url, self._client, _request_headers(url, self._settings))

    async def parse_public(self, url: str) -> LinkMetadata:
        # Omitting our Cookie header alone is insufficient: the shared client
        # may already contain cookies from prior responses and redirect hops.
        async with httpx.AsyncClient(timeout=self._client.timeout) as client:
            return await self._parse(url, client, None)

    async def _parse(
        self, url: str, client: httpx.AsyncClient, headers: dict[str, str] | None,
    ) -> LinkMetadata:
        platform = detect_platform(url)
        try:
            resp = await get_response(
                client, url, source_url=url, label="og meta", headers=headers,
            )
        except ParserError as exc:
            logger.warning(
                "og meta failed: platform=%s url=%s reason=%s", platform, url, exc.reason,
            )
            raise

        soup = BeautifulSoup(resp.text, "lxml")
        meta = _extract_og(soup)

        domain = re.sub(r"^www\.", "", urlparse(url).netloc)
        title = meta.get("og:title") or _tag_text(soup, "title") or domain
        description = meta.get("og:description", "")
        canonical = meta.get("og:url", "")
        if canonical and content_key(canonical) != content_key(url):
            canonical = ""
        covers = list(dict.fromkeys(str(tag.get("content")) for tag in soup.find_all("meta")
                                   if tag.get("property") in {"og:image", "og:image:secure_url"}
                                   and tag.get("content")))[:3]
        result = LinkMetadata(
            source_url=url,
            title=title,
            description=description,
            cover_url=covers[0] if covers else "",
            site_name=meta.get("og:site_name") or domain,
            platform=platform,
            canonical_url=canonical,
            media_type=MediaType.VIDEO if "video" in meta.get("og:type", "") else MediaType.ARTICLE,
            cover_candidates=covers,
            has_visual=True if covers else None,
            content_verified=(
                not is_placeholder(meta.get("og:title", ""), platform, url)
                or not is_placeholder(description, platform, url)
            ),
        )
        if platform not in {"youtube", "instagram", "x", "tiktok", "douyin"}:
            return result
        # The fetched page is also the fallback document: do not request it
        # again just to inspect structured data, title, or a different meta tag.
        from .social_page import _image_index, page_identity, parse_page_metadata

        if platform == "instagram" and (_image_index(url) or 1) > 1:
            # OG only identifies the post's default image, never a requested slide.
            result.cover_url = ""
            result.cover_candidates = []

        content_url = url
        if not page_identity(url)[1]:
            # A short link can visit the original unavailable post before
            # redirecting to a recommendation. Preserve its first content ID.
            for redirect in [*resp.history, resp]:
                candidates = [str(redirect.url)]
                if location := redirect.headers.get("location"):
                    candidates.append(urljoin(str(redirect.url), location))
                locked = next((candidate for candidate in candidates
                               if page_identity(candidate)[0] == platform
                               and page_identity(candidate)[1]), "")
                if locked:
                    content_url = locked
                    break
        try:
            structured = parse_page_metadata(content_url, resp.text, final_url=str(resp.url))
        except ParserError as exc:
            if "unsupported" in exc.reason.lower():
                return result
            raise ParserError(url, exc.reason) from exc
        structured.source_url = url
        merge_metadata(structured, result)
        return structured


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


def _request_headers(url: str, settings: Settings | None) -> dict[str, str]:
    headers = {"User-Agent": _USER_AGENT}
    if settings is None:
        return headers
    domain = urlparse(url).netloc
    cookie_file = settings.cookie_file_for_platform(detect_platform(url))
    cookie_header = get_cookie_header(cookie_file, domain)
    if cookie_header:
        headers["Cookie"] = cookie_header
    return headers
