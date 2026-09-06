import json
from pathlib import Path

import httpx
import respx

from src.config import Settings
from src.parsers.base import MediaType
from src.parsers.x_graphql import XGraphQLParser


@respx.mock
async def test_x_graphql_extracts_post_image_and_counts() -> None:
    route = respx.get(
        "https://x.com/i/api/graphql/2Acdg-VztGlHX7MjX67Ysw/TweetResultByRestId"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "tweetResult": {
                        "result": {
                            "legacy": {
                                "full_text": "hello https://t.co/photo",
                                "favorite_count": 57,
                                "reply_count": 6,
                                "retweet_count": 7,
                                "quote_count": 8,
                                "extended_entities": {
                                    "media": [{
                                        "type": "photo",
                                        "media_url_https": (
                                            "https://pbs.twimg.com/media/example.jpg"
                                        ),
                                    }],
                                },
                            },
                            "views": {"count": "1234"},
                        },
                    },
                },
            },
        )
    )
    settings = Settings(cookie_file="tests/fixtures/x-cookie.txt")

    async with httpx.AsyncClient() as client:
        meta = await XGraphQLParser(client, settings).parse(
            "https://x.com/NFTCPS/status/2056556650123956505"
        )

    assert route.called
    assert meta.cover_url == "https://pbs.twimg.com/media/example.jpg"
    assert meta.description == "hello https://t.co/photo"
    assert meta.channel == "@NFTCPS"
    assert meta.view_count == 1234
    assert meta.like_count == 57
    assert meta.comment_count == 6
    assert meta.repost_count == 7


@respx.mock
async def test_x_graphql_accepts_text_without_media() -> None:
    route = respx.get(
        "https://x.com/i/api/graphql/2Acdg-VztGlHX7MjX67Ysw/TweetResultByRestId"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "tweetResult": {
                        "result": {
                            "legacy": {
                                "full_text": "plain text post",
                                "favorite_count": 1,
                                "reply_count": 2,
                                "retweet_count": 3,
                            },
                        },
                    },
                },
            },
        )
    )
    settings = Settings(cookie_file="tests/fixtures/x-cookie.txt")

    async with httpx.AsyncClient() as client:
        meta = await XGraphQLParser(client, settings).parse(
            "https://x.com/NFTCPS/status/2056556650123956505/video/1"
        )

    assert route.called
    assert meta.description == "plain text post"
    assert meta.cover_url == ""
    assert meta.channel == "@NFTCPS"
    assert meta.like_count == 1


@respx.mock
async def test_native_article_uses_preview_fields_without_requesting_article_body() -> None:
    payload = json.loads(Path("tests/fixtures/x-native-article.json").read_text())
    route = respx.get(
        "https://x.com/i/api/graphql/2Acdg-VztGlHX7MjX67Ysw/TweetResultByRestId"
    ).mock(return_value=httpx.Response(200, json=payload))
    settings = Settings(cookie_file="tests/fixtures/x-cookie.txt")
    async with httpx.AsyncClient() as client:
        meta = await XGraphQLParser(client, settings).parse("https://x.com/author/status/123")
    features = json.loads(route.calls.last.request.url.params["features"])
    toggles = json.loads(route.calls.last.request.url.params["fieldToggles"])
    assert features["responsive_web_twitter_article_tweet_consumption_enabled"] is True
    assert toggles == {"withArticleRichContentState": False, "withArticlePlainText": False}
    assert meta.title == "A field guide to reliable systems"
    assert meta.description == "An independent review of practical engineering observations."
    assert meta.cover_url == "https://pbs.twimg.com/media/example-article.jpg"
    assert meta.has_visual is True
    assert meta.media_type == MediaType.ARTICLE
    assert meta.canonical_url == "https://x.com/author/status/123"
