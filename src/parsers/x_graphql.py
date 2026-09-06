from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from ..config import Settings
from ..cookie_utils import cookie_value, get_cookie_header
from .base import LinkMetadata, MediaType, ParserError
from .card_http import get_response, json_object

_BEARER_TOKEN = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
_QUERY_ID = "2Acdg-VztGlHX7MjX67Ysw"
_FEATURES = {
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
}
_FIELD_TOGGLES = {"withArticleRichContentState": False, "withArticlePlainText": False}


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

        cookie_file = self._settings.cookie_file_for_platform("x")
        cookie_header = get_cookie_header(cookie_file, "x.com")
        csrf_token = cookie_value(cookie_header, "ct0")
        if not cookie_header or not csrf_token:
            raise ParserError(url, "x cookie is not configured")

        variables = {
            "tweetId": tweet_id,
            "withCommunity": False,
            "includePromotedContent": False,
            "withVoice": False,
        }
        endpoint = x_graphql_endpoint(
            _QUERY_ID, "TweetResultByRestId", variables, _FEATURES, _FIELD_TOGGLES,
        )
        resp = await get_response(
            self._client, endpoint, source_url=url, label="x GraphQL",
            headers=x_api_headers(url, cookie_header, csrf_token),
        )
        data = json_object(resp, url, "x GraphQL")

        result = _tweet_result(data)
        if not result:
            raise ParserError(url, "x GraphQL returned no tweet")
        if result.get("rest_id") is not None and str(result["rest_id"]) != tweet_id:
            raise ParserError(url, "target_mismatch: X GraphQL returned another post")

        legacy = result.get("legacy") if isinstance(result.get("legacy"), dict) else {}
        cover_url = _cover_url_from_legacy(legacy)
        note = result.get("note_tweet")
        note_results = note.get("note_tweet_results") if isinstance(note, dict) else None
        note_result = note_results.get("result") if isinstance(note_results, dict) else None
        full_text = str(
            (note_result.get("text") if isinstance(note_result, dict) else None)
            or legacy.get("full_text") or ""
        )
        article = _article_result(result)
        article_title = _text(article.get("title"))
        preview = _text(article.get("preview_text"))
        article_cover = _article_cover(article)
        covers = list(dict.fromkeys(
            ([article_cover] if article_cover else []) + _cover_candidates(legacy)
        ))[:3]
        if article_cover:
            cover_url = article_cover
        if not full_text and not cover_url and not article_title and not preview:
            raise ParserError(url, "x GraphQL returned no usable tweet content")
        has_visual: bool | None = bool(_media_items(legacy))
        if article:
            has_visual = True if article_cover or article.get("cover_media") else (
                False if "cover_media" in article else None
            )

        return LinkMetadata(
            source_url=url,
            title=article_title or "X Post",
            description=preview or full_text,
            cover_url=cover_url,
            site_name="X",
            platform="x",
            canonical_url=url,
            media_type=MediaType.VIDEO if _has_video(legacy) and not article else MediaType.ARTICLE,
            channel=_handle_from_url(url),
            view_count=_view_count(result),
            like_count=_as_int(legacy.get("favorite_count")),
            comment_count=_as_int(legacy.get("reply_count")),
            repost_count=_as_int(legacy.get("retweet_count") or legacy.get("quote_count")),
            cover_candidates=covers,
            has_visual=has_visual,
            content_verified=True,
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
    nested = data.get("data")
    tweet_result = nested.get("tweetResult") if isinstance(nested, dict) else None
    result = tweet_result.get("result") if isinstance(tweet_result, dict) else None
    if isinstance(result, dict) and isinstance(result.get("tweet"), dict):
        result = result["tweet"]
    return result if isinstance(result, dict) else {}


def _article_result(result: dict[str, Any]) -> dict[str, Any]:
    article = result.get("article")
    results = article.get("article_results") if isinstance(article, dict) else None
    item = results.get("result") if isinstance(results, dict) else None
    return item if isinstance(item, dict) else {}


def _article_cover(article: dict[str, Any]) -> str:
    cover = article.get("cover_media")
    info = cover.get("media_info") if isinstance(cover, dict) else None
    return _text(info.get("original_img_url")) if isinstance(info, dict) else ""


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _media_items(legacy: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("extended_entities", "entities"):
        container = legacy.get(key)
        items = container.get("media") if isinstance(container, dict) else None
        if isinstance(items, list) and items:
            return [item for item in items if isinstance(item, dict)]
    return []


def _has_video(legacy: dict[str, Any]) -> bool:
    return any(item.get("type") in {"video", "animated_gif"} for item in _media_items(legacy))


def _cover_candidates(legacy: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(
        str(url) for item in _media_items(legacy)
        if (url := item.get("media_url_https") or item.get("media_url"))
    ))[:3]


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
