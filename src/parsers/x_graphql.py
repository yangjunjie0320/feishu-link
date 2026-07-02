from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from ..config import Settings
from ..cookie_utils import cookie_value, get_cookie_header
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


def x_graphql_endpoint(
    query_id: str,
    operation: str,
    variables: dict[str, Any],
    features: dict[str, Any],
    field_toggles: dict[str, Any] | None = None,
) -> str:
    params = {
        "variables": json.dumps(variables, separators=(",", ":")),
        "features": json.dumps(features, separators=(",", ":")),
    }
    if field_toggles is not None:
        params["fieldToggles"] = json.dumps(field_toggles, separators=(",", ":"))
    return f"https://x.com/i/api/graphql/{query_id}/{operation}?" + urlencode(params)


def x_api_headers(
    url: str,
    cookie_header: str,
    csrf_token: str,
    *,
    user_agent: str = "Mozilla/5.0",
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_BEARER_TOKEN}",
        "Cookie": cookie_header,
        "Referer": url,
        "User-Agent": user_agent,
        "X-CSRF-Token": csrf_token,
        "X-Twitter-Active-User": "yes",
        "X-Twitter-Auth-Type": "OAuth2Session",
        "X-Twitter-Client-Language": "en",
    }


class XGraphQLParser:
    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def parse(self, url: str) -> LinkMetadata:
        tweet_id = tweet_id_from_url(url)
        if not tweet_id:
            raise ParserError(url, "x tweet id not found")

        domain = urlparse(url).netloc
        cookie_file = self._settings.cookie_file_for_platform("x")
        cookie_header = get_cookie_header(cookie_file, domain)
        csrf_token = cookie_value(cookie_header, "ct0")
        if not cookie_header or not csrf_token:
            raise ParserError(url, "x cookie is not configured")

        variables = {
            "tweetId": tweet_id,
            "withCommunity": False,
            "includePromotedContent": False,
            "withVoice": False,
        }
        endpoint = x_graphql_endpoint(_QUERY_ID, "TweetResultByRestId", variables, _FEATURES)
        try:
            resp = await self._client.get(
                endpoint,
                headers=x_api_headers(url, cookie_header, csrf_token),
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
        full_text = str(legacy.get("full_text") or "")
        if not full_text and not cover_url:
            raise ParserError(url, "x GraphQL returned no usable tweet content")

        return LinkMetadata(
            source_url=url,
            title="X Post",
            description=full_text,
            cover_url=cover_url,
            site_name="X",
            platform="x",
            canonical_url=url,
            media_type=MediaType.ARTICLE,
            channel=_handle_from_url(url),
            view_count=_view_count(result),
            like_count=_as_int(legacy.get("favorite_count")),
            comment_count=_as_int(legacy.get("reply_count")),
            repost_count=_as_int(legacy.get("retweet_count") or legacy.get("quote_count")),
        )


def tweet_id_from_url(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if "status" not in parts:
        return ""
    index = parts.index("status")
    if index + 1 >= len(parts):
        return ""
    return parts[index + 1]


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


def _handle_from_url(url: str) -> str | None:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if not parts:
        return None
    if parts[0].lower() in {"i", "intent"}:
        return None
    return f"@{parts[0]}"
