from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from ..config import Settings
from ..cookie_utils import cookie_header_from_netscape_file
from .base import LinkMetadata, MediaType, ParserError

_BEARER_TOKEN = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
_QUERY_ID = "2Acdg-VztGlHX7MjX67Ysw"
_FEATURES = {
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
}


class XGraphQLParser:
    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def parse(self, url: str) -> LinkMetadata:
        tweet_id = _tweet_id_from_url(url)
        if not tweet_id:
            raise ParserError(url, "x tweet id not found")

        cookie_header = cookie_header_from_netscape_file(
            self._settings.cookie_file_for_platform("x")
        )
        csrf_token = _cookie_value(cookie_header, "ct0")
        if not cookie_header or not csrf_token:
            raise ParserError(url, "x cookie is not configured")

        variables = {
            "tweetId": tweet_id,
            "withCommunity": False,
            "includePromotedContent": False,
            "withVoice": False,
        }
        endpoint = (
            f"https://x.com/i/api/graphql/{_QUERY_ID}/TweetResultByRestId?"
            + urlencode({
                "variables": json.dumps(variables, separators=(",", ":")),
                "features": json.dumps(_FEATURES, separators=(",", ":")),
            })
        )
        try:
            resp = await self._client.get(
                endpoint,
                headers={
                    "Authorization": f"Bearer {_BEARER_TOKEN}",
                    "Cookie": cookie_header,
                    "Referer": url,
                    "User-Agent": "Mozilla/5.0",
                    "X-CSRF-Token": csrf_token,
                    "X-Twitter-Active-User": "yes",
                    "X-Twitter-Auth-Type": "OAuth2Session",
                    "X-Twitter-Client-Language": "en",
                },
                follow_redirects=True,
            )
        except httpx.RequestError as e:
            raise ParserError(url, f"x GraphQL request error: {e}") from e

        if resp.status_code >= 400:
            raise ParserError(url, f"x GraphQL HTTP {resp.status_code}")

        try:
            data: dict[str, Any] = resp.json()
        except ValueError as e:
            raise ParserError(url, "x GraphQL returned non-json response") from e

        result = _tweet_result(data)
        if not result:
            raise ParserError(url, "x GraphQL returned no tweet")

        legacy = result.get("legacy") if isinstance(result.get("legacy"), dict) else {}
        cover_url = _cover_url_from_legacy(legacy)
        if not cover_url:
            raise ParserError(url, "x GraphQL returned no media image")

        return LinkMetadata(
            source_url=url,
            title="X Post",
            description=str(legacy.get("full_text") or ""),
            cover_url=cover_url,
            site_name="X",
            platform="x",
            canonical_url=url,
            media_type=MediaType.ARTICLE,
            view_count=_view_count(result),
            like_count=_as_int(legacy.get("favorite_count")),
            comment_count=_as_int(legacy.get("reply_count")),
            repost_count=_as_int(legacy.get("retweet_count") or legacy.get("quote_count")),
        )


def _tweet_id_from_url(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if "status" not in parts:
        return ""
    index = parts.index("status")
    if index + 1 >= len(parts):
        return ""
    return parts[index + 1]


def _cookie_value(cookie_header: str, name: str) -> str:
    for part in cookie_header.split("; "):
        if part.startswith(f"{name}="):
            return part.split("=", 1)[1]
    return ""


def _tweet_result(data: dict[str, Any]) -> dict[str, Any]:
    result = data.get("data", {}).get("tweetResult", {}).get("result", {})
    return result if isinstance(result, dict) else {}


def _cover_url_from_legacy(legacy: dict[str, Any]) -> str:
    containers = (legacy.get("extended_entities"), legacy.get("entities"))
    for container in containers:
        if not isinstance(container, dict):
            continue
        media_items = container.get("media")
        if not isinstance(media_items, list):
            continue
        for media in media_items:
            if not isinstance(media, dict):
                continue
            if media.get("type") != "photo":
                continue
            url = media.get("media_url_https") or media.get("media_url")
            if url:
                return str(url)
    return ""


def _view_count(result: dict[str, Any]) -> int | None:
    views = result.get("views")
    if not isinstance(views, dict):
        return None
    return _as_int(views.get("count"))


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None
