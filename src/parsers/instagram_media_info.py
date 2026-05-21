from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from ..config import Settings
from .base import LinkMetadata, MediaType, ParserError
from .og_meta import _request_headers

_SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


class InstagramMediaInfoParser:
    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def parse(self, url: str) -> LinkMetadata:
        shortcode = _shortcode_from_url(url)
        if not shortcode:
            raise ParserError(url, "instagram shortcode not found")

        endpoint = (
            "https://www.instagram.com/api/v1/media/"
            f"{_shortcode_to_media_id(shortcode)}/info/"
        )
        headers = _request_headers("instagram", self._settings)
        headers.update({
            "Referer": "https://www.instagram.com/",
            "X-IG-App-ID": "936619743392459",
        })

        try:
            resp = await self._client.get(endpoint, headers=headers, follow_redirects=True)
        except httpx.RequestError as e:
            raise ParserError(url, f"instagram media info request error: {e}") from e

        if resp.status_code >= 400:
            raise ParserError(url, f"instagram media info HTTP {resp.status_code}")

        try:
            data: dict[str, Any] = resp.json()
        except ValueError as e:
            raise ParserError(url, "instagram media info returned non-json response") from e

        item = _first_item(data)
        if not item:
            raise ParserError(url, "instagram media info returned no items")

        cover_url = _cover_url_from_item(item, _img_index(url))
        if not cover_url:
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
