from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from ..config import Settings
from ..http_errors import describe_request_error
from .base import LinkMetadata, MediaType, ParserError
from .og_meta import _request_headers

logger = logging.getLogger(__name__)

_SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

# Instagram answers 4xx with a JSON body explaining itself ("login_required",
# "Please wait a few minutes before you try again"). Without it a bare status
# code cannot tell a rate limit from a dead cookie.
_ERROR_BODY_CHARS = 200


class InstagramMediaInfoParser:
    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def parse(self, url: str) -> LinkMetadata:
        shortcode = _shortcode_from_url(url)
        if not shortcode:
            raise ParserError(url, "instagram shortcode not found")

        endpoint = (
            f"https://www.instagram.com/api/v1/media/{_shortcode_to_media_id(shortcode)}/info/"
        )
        headers = _request_headers("https://www.instagram.com/", self._settings)
        headers.update(
            {
                "Referer": "https://www.instagram.com/",
                "X-IG-App-ID": "936619743392459",
            }
        )

        has_cookie = "Cookie" in headers
        logger.info("instagram media info request: shortcode=%s cookie=%s", shortcode, has_cookie)

        try:
            resp = await self._client.get(endpoint, headers=headers, follow_redirects=True)
        except httpx.RequestError as e:
            logger.warning(
                "instagram media info transport error: shortcode=%s cookie=%s error=%s",
                shortcode,
                has_cookie,
                describe_request_error(e),
            )
            raise ParserError(
                url, f"instagram media info request error: {describe_request_error(e)}"
            ) from e

        if resp.status_code >= 400:
            logger.warning(
                "instagram media info HTTP %d: shortcode=%s cookie=%s content_type=%s body=%r",
                resp.status_code,
                shortcode,
                has_cookie,
                resp.headers.get("content-type", ""),
                resp.text[:_ERROR_BODY_CHARS],
            )
            raise ParserError(url, f"instagram media info HTTP {resp.status_code}")

        try:
            data: dict[str, Any] = resp.json()
        except ValueError as e:
            logger.warning(
                "instagram media info non-json: shortcode=%s content_type=%s body=%r",
                shortcode,
                resp.headers.get("content-type", ""),
                resp.text[:_ERROR_BODY_CHARS],
            )
            raise ParserError(url, "instagram media info returned non-json response") from e

        item = _first_item(data)
        if not item:
            logger.warning(
                "instagram media info no items: shortcode=%s keys=%s",
                shortcode,
                sorted(data.keys())[:10],
            )
            raise ParserError(url, "instagram media info returned no items")

        cover_url = _cover_url_from_item(item, _img_index(url))
        if not cover_url:
            logger.warning(
                "instagram media info no image: shortcode=%s media_type=%s carousel=%s",
                shortcode,
                item.get("media_type"),
                isinstance(item.get("carousel_media"), list),
            )
            raise ParserError(url, "instagram media info returned no image")

        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        caption = item.get("caption") if isinstance(item.get("caption"), dict) else {}
        return LinkMetadata(
            source_url=url,
            title=f"Post by {user.get('username')}" if user.get("username") else "Instagram Post",
            description=str(caption.get("text") or ""),
            cover_url=cover_url,
            site_name="Instagram",
            platform="instagram",
            media_type=MediaType.ARTICLE,
            channel=str(user.get("full_name") or user.get("username") or "") or None,
            like_count=_as_int(item.get("like_count")),
            comment_count=_as_int(item.get("comment_count")),
            repost_count=_as_int(item.get("media_repost_count")),
        )


def _shortcode_from_url(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if len(parts) < 2:
        return ""
    if parts[0].lower() not in {"p", "tv", "reel", "reels"}:
        return ""
    return parts[1]


def _shortcode_to_media_id(shortcode: str) -> int:
    if len(shortcode) > 28:
        shortcode = shortcode[:-28]
    result = 0
    for char in shortcode:
        result = result * 64 + _SHORTCODE_ALPHABET.index(char)
    return result


def _img_index(url: str) -> int | None:
    raw = parse_qs(urlparse(url).query).get("img_index", [""])[0]
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _first_item(data: dict[str, Any]) -> dict[str, Any]:
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return {}
    first = items[0]
    return first if isinstance(first, dict) else {}


def _cover_url_from_item(item: dict[str, Any], img_index: int | None) -> str:
    media_items = item.get("carousel_media")
    if isinstance(media_items, list) and media_items:
        if img_index is not None and img_index <= len(media_items):
            selected = media_items[img_index - 1]
            if isinstance(selected, dict):
                cover_url = _image_url_from_media(selected)
                if cover_url:
                    return cover_url
        for media_item in media_items:
            if isinstance(media_item, dict):
                cover_url = _image_url_from_media(media_item)
                if cover_url:
                    return cover_url

    return _image_url_from_media(item)


def _image_url_from_media(media: dict[str, Any]) -> str:
    candidates = (
        media.get("image_versions2", {}).get("candidates", [])
        if isinstance(media.get("image_versions2"), dict)
        else []
    )
    if not isinstance(candidates, list):
        return ""
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate.get("url"):
            return str(candidate["url"])
    return ""


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None
