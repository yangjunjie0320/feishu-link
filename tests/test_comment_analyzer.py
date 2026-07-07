import sys
import types

import httpx
import respx

from src.comment_analyzer import (
    CommentAnalysisError,
    CommentAnalyzer,
    CommentInsight,
    VideoComment,
    _comment_fetch_limit,
    build_comment_analysis_prompt,
    comments_from_raw,
    parse_comment_insight,
    render_comment_analysis_markdown,
    select_top_comments,
    sort_comments_by_heat,
)
from src.config import Settings
from src.cookie_utils import get_cookie_header
from src.parsers.instagram_media_info import _shortcode_to_media_id


def test_comments_from_raw_cleans_dedupes_and_counts_replies() -> None:
    comments = comments_from_raw(
        [
            {
                "id": "root",
                "author": "Alice",
                "text": "<b>Hello</b> world",
                "like_count": "10",
            },
            {
                "id": "reply",
                "parent": "root",
                "author": "Bob",
                "text": "Reply &amp; detail",
                "like_count": 3,
            },
            {
                "id": "dup",
                "author": "Alice",
                "text": "Hello world",
                "like_count": 1,
            },
        ],
        max_comments=10,
    )

    assert len(comments) == 2
    assert comments[0].text == "Hello world"
    assert comments[0].reply_count == 1
    assert comments[1].text == "Reply & detail"


def test_comments_from_raw_handles_instagram_user_and_child_comments() -> None:
    comments = comments_from_raw(
        [
            {
                "pk": "root",
                "user": {"username": "alice"},
                "text": "Root",
                "comment_like_count": 12,
                "child_comment_count": 4,
                "preview_child_comments": [
                    {
                        "pk": "child",
                        "user": {"username": "bob"},
                        "text": "Child",
                        "comment_like_count": 2,
                    }
                ],
            }
        ],
        max_comments=10,
    )

    assert len(comments) == 2
    assert comments[0].author == "alice"
    assert comments[0].like_count == 12
    assert comments[0].reply_count == 4
    assert comments[1].author == "bob"
    assert comments[1].parent_id == "root"


@respx.mock
async def test_fetch_instagram_comments_uses_media_comments_api() -> None:
    media_id = _shortcode_to_media_id("DYfWbunGlNg")
    route = respx.get(f"https://www.instagram.com/api/v1/media/{media_id}/comments/").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "comments": [
                        {
                            "pk": "c1",
                            "user": {"username": "alice"},
                            "text": "First",
                            "comment_like_count": 9,
                        }
                    ],
                    "comment_count": 42,
                    "has_more_comments": True,
                    "next_max_id": "page2",
                },
            ),
            httpx.Response(
                200,
                json={
                    "comments": [
                        {
                            "pk": "c2",
                            "user": {"username": "bob"},
                            "text": "Second",
                            "comment_like_count": 4,
                        }
                    ],
                    "has_more_comments": False,
                },
            ),
        ]
    )

    async with httpx.AsyncClient() as client:
        fetched = await CommentAnalyzer(Settings(), client).fetch_comment_page(
            "https://www.instagram.com/p/DYfWbunGlNg/"
        )

    comments = fetched.comments
    assert route.call_count == 2
    assert route.calls[0].request.url.params["can_support_threading"] == "true"
    assert route.calls[1].request.url.params["max_id"] == "page2"
    assert fetched.total_count == 42
    assert [comment.text for comment in comments] == ["First", "Second"]
    assert comments[0].author == "alice"


async def test_analyze_times_out_slow_comment_fetch(monkeypatch) -> None:
    import asyncio

    async def slow_fetch(url: str):
        await asyncio.sleep(5)

    async with httpx.AsyncClient() as client:
        analyzer = CommentAnalyzer(Settings(comment_fetch_timeout=0.05), client)
        monkeypatch.setattr(analyzer, "fetch_comment_page", slow_fetch)
        try:
            await analyzer.analyze("https://www.youtube.com/watch?v=abc")
        except CommentAnalysisError as e:
            assert "超时" in str(e)
        else:
            raise AssertionError("expected CommentAnalysisError on timeout")


@respx.mock
async def test_instagram_non_post_url_rejected_without_ytdlp_fallback() -> None:
    # Profile links carry no shortcode; they must fail fast with a friendly
    # message instead of falling back to yt-dlp (its IG extractor is broken).
    async with httpx.AsyncClient() as client:
        analyzer = CommentAnalyzer(Settings(), client)
        try:
            await analyzer.fetch_comment_page("https://www.instagram.com/andrettife?igsh=abc")
        except CommentAnalysisError as e:
            assert "不是 Instagram 帖子/Reel" in str(e)
        else:
            raise AssertionError("expected CommentAnalysisError")
    assert respx.calls.call_count == 0


@respx.mock
async def test_fetch_instagram_comments_sends_cookie_and_web_headers(tmp_path) -> None:
    cookie_file = tmp_path / "instagram.txt"
    cookie_file.write_text(
        "\n".join(
            [
                "# Netscape HTTP Cookie File",
                ".instagram.com\tTRUE\t/\tTRUE\t1800000000\tsessionid\tsecret-session",
                ".instagram.com\tTRUE\t/\tTRUE\t1800000000\tcsrftoken\tcsrf-token",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    media_id = _shortcode_to_media_id("DYfWbunGlNg")
    respx.get("https://www.instagram.com/p/DYfWbunGlNg/").mock(
        return_value=httpx.Response(
            200,
            text='{"rollout_hash":"rollout-1","claim":"claim-1"}',
        )
    )
    route = respx.get(f"https://www.instagram.com/api/v1/media/{media_id}/comments/").mock(
        return_value=httpx.Response(
            200,
            json={
                "comments": [
                    {"pk": "c1", "user": {"username": "alice"}, "text": "First"},
                ],
                "has_more_comments": False,
            },
        )
    )
    settings = Settings(platform_cookie_files={"instagram": str(cookie_file)})

    async with httpx.AsyncClient() as client:
        await CommentAnalyzer(settings, client).fetch_comment_page(
            "https://www.instagram.com/p/DYfWbunGlNg/"
        )

    headers = route.calls[0].request.headers
    assert "sessionid=secret-session" in headers["cookie"]
    assert headers["x-csrftoken"] == "csrf-token"
    assert headers["x-ig-app-id"] == "936619743392459"
    assert headers["x-asbd-id"] == "129477"
    assert headers["x-instagram-ajax"] == "rollout-1"
    assert headers["x-ig-www-claim"] == "claim-1"


@respx.mock
async def test_fetch_x_comments_uses_tweet_detail_graphql(tmp_path) -> None:
    cookie_file = tmp_path / "x.txt"
    cookie_file.write_text(
        "\n".join(
            [
                "# Netscape HTTP Cookie File",
                ".x.com\tTRUE\t/\tTRUE\t1800000000\tct0\tcsrf-token",
                ".x.com\tTRUE\t/\tTRUE\t1800000000\tauth_token\tsecret-auth",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    route = respx.get("https://x.com/i/api/graphql/jd3V43oDY9cY7obs1YMfbQ/TweetDetail").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "threaded_conversation_with_injections_v2": {
                        "instructions": [
                            {
                                "entries": [
                                    {
                                        "entryId": "tweet-123",
                                        "content": {
                                            "itemContent": {
                                                "tweet_results": {
                                                    "result": _x_tweet(
                                                        "123",
                                                        "root post",
                                                        reply_count=2,
                                                    )
                                                }
                                            }
                                        },
                                    },
                                    {
                                        "entryId": "conversationthread-456",
                                        "content": {
                                            "items": [
                                                {
                                                    "item": {
                                                        "itemContent": {
                                                            "tweet_results": {
                                                                "result": _x_tweet(
                                                                    "456",
                                                                    "first reply",
                                                                    favorite_count=15,
                                                                    reply_count=1,
                                                                    parent_id="123",
                                                                )
                                                            }
                                                        }
                                                    }
                                                }
                                            ]
                                        },
                                    },
                                ]
                            }
                        ]
                    }
                },
            },
        )
    )
    settings = Settings(platform_cookie_files={"x": str(cookie_file)})

    async with httpx.AsyncClient() as client:
        fetched = await CommentAnalyzer(settings, client).fetch_comment_page(
            "https://x.com/alice/status/123"
        )

    assert route.called
    headers = route.calls[0].request.headers
    assert "auth_token=secret-auth" in headers["cookie"]
    assert headers["x-csrf-token"] == "csrf-token"
    assert fetched.total_count == 2
    assert len(fetched.comments) == 1
    assert fetched.comments[0].text == "first reply"
    assert fetched.comments[0].author == "@bob"
    assert fetched.comments[0].author_url == "https://x.com/bob"
    assert fetched.comments[0].comment_url == "https://x.com/bob/status/456"


async def test_ytdlp_comment_fetch_keeps_late_high_like_comments(monkeypatch) -> None:
    class FakeYoutubeDL:
        def __init__(self, options: dict[str, object]) -> None:
            self.options = options

        def __enter__(self) -> "FakeYoutubeDL":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def extract_info(self, url: str, download: bool = False) -> dict[str, object]:
            return {
                "id": "abc",
                "extractor_key": "Youtube",
                "comment_count": 4,
                "comments": [
                    {"id": "c1", "author": "a", "text": "early low", "like_count": 1},
                    {"id": "c2", "author": "b", "text": "second low", "like_count": 2},
                    {"id": "c3", "author": "c", "text": "late high", "like_count": 99},
                    {"id": "c4", "author": "d", "text": "late warm", "like_count": 50},
                ],
            }

    fake_module = types.SimpleNamespace(YoutubeDL=FakeYoutubeDL)
    monkeypatch.setitem(sys.modules, "yt_dlp", fake_module)

    async with httpx.AsyncClient() as client:
        fetched = await CommentAnalyzer(
            Settings(comment_analysis_max_comments=2),
            client,
        ).fetch_comment_page("https://www.youtube.com/watch?v=abc")

    assert [comment.text for comment in fetched.comments] == ["late high", "late warm"]


@respx.mock
async def test_fetch_bilibili_comments_uses_wbi_reply_api() -> None:
    view_route = respx.get("https://api.bilibili.com/x/web-interface/view").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "aid": 116623813904633,
                    "stat": {"reply": 5279},
                },
            },
        )
    )
    nav_route = respx.get("https://api.bilibili.com/x/web-interface/nav").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "wbi_img": {
                        "img_url": "https://i0.hdslb.com/bfs/wbi/abcdefghijklmnopqrstuvwxyzabcdef.png",
                        "sub_url": "https://i0.hdslb.com/bfs/wbi/ghijklmnopqrstuvwxyzabcdefghijkl.png",
                    }
                }
            },
        )
    )
    reply_route = respx.get("https://api.bilibili.com/x/v2/reply/wbi/main").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "cursor": {
                            "is_end": False,
                            "all_count": 5279,
                            "pagination_reply": {"next_offset": "page-2"},
                        },
                        "replies": [
                            {
                                "rpid": 101,
                                "member": {"uname": "alice"},
                                "content": {"message": "第一条"},
                                "like": 88,
                                "rcount": 1,
                                "replies": [
                                    {
                                        "rpid": 102,
                                        "parent": 101,
                                        "member": {"uname": "bob"},
                                        "content": {"message": "回复 &amp; 详情"},
                                        "like": 7,
                                    }
                                ],
                            }
                        ],
                    },
                },
            ),
            httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "cursor": {
                            "is_end": True,
                            "all_count": 5279,
                            "pagination_reply": {"next_offset": ""},
                        },
                        "replies": [
                            {
                                "rpid": 103,
                                "member": {"uname": "carol"},
                                "content": {"message": "第二页"},
                                "like": 5,
                            }
                        ],
                    },
                },
            ),
        ]
    )

    async with httpx.AsyncClient() as client:
        analyzer = CommentAnalyzer(Settings(comment_analysis_max_comments=20), client)
        fetched = await analyzer.fetch_comment_page("https://www.bilibili.com/video/BV1BCGB66E8P/")

    assert view_route.called
    assert view_route.calls[0].request.url.params["bvid"] == "BV1BCGB66E8P"
    assert nav_route.called
    assert reply_route.call_count == 2
    assert reply_route.calls[0].request.url.params["oid"] == "116623813904633"
    assert reply_route.calls[0].request.url.params["mode"] == "3"
    assert reply_route.calls[0].request.url.params["pagination_str"] == '{"offset":""}'
    assert reply_route.calls[0].request.url.params["w_rid"]
    assert reply_route.calls[1].request.url.params["pagination_str"] == '{"offset":"page-2"}'
    assert fetched.total_count == 5279
    assert len(fetched.comments) == 3
    assert fetched.comments[0].text == "第一条"
    assert fetched.comments[0].author == "alice"
    assert fetched.comments[0].like_count == 88
    assert fetched.comments[0].reply_count == 1
    assert fetched.comments[1].text == "回复 & 详情"
    assert fetched.comments[1].parent_id == "101"
    assert fetched.comments[2].text == "第二页"


@respx.mock
async def test_fetch_bilibili_comments_sends_no_cookie(tmp_path) -> None:
    cookie_file = tmp_path / "bilibili.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\tsecret-session\n",
        encoding="utf-8",
    )
    # The cookie is available for this domain; it must still not be sent.
    assert get_cookie_header(str(cookie_file), "www.bilibili.com")

    respx.get("https://api.bilibili.com/x/web-interface/view").mock(
        return_value=httpx.Response(
            200,
            json={"code": 0, "data": {"aid": 1, "stat": {"reply": 1}}},
        )
    )
    respx.get("https://api.bilibili.com/x/web-interface/nav").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "wbi_img": {
                        "img_url": "https://i0.hdslb.com/bfs/wbi/abcdefghijklmnopqrstuvwxyzabcdef.png",
                        "sub_url": "https://i0.hdslb.com/bfs/wbi/ghijklmnopqrstuvwxyzabcdefghijkl.png",
                    }
                }
            },
        )
    )
    reply_route = respx.get("https://api.bilibili.com/x/v2/reply/wbi/main").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "cursor": {"is_end": True, "all_count": 1, "pagination_reply": {}},
                    "replies": [
                        {"rpid": 1, "member": {"uname": "a"}, "content": {"message": "hi"}}
                    ],
                },
            },
        )
    )

    settings = Settings(platform_cookie_files={"bilibili": str(cookie_file)})
    async with httpx.AsyncClient() as client:
        analyzer = CommentAnalyzer(settings, client)
        await analyzer.fetch_comment_page("https://www.bilibili.com/video/BV1BCGB66E8P/")

    for route in respx.routes:
        for call in route.calls:
            assert "cookie" not in {k.lower() for k in call.request.headers}
    assert reply_route.called


@respx.mock
async def test_fetch_bilibili_reply_isolated_from_shared_cookie_jar() -> None:
    # A buvid3 in the shared client's cookie jar freezes bilibili heat-mode
    # pagination; the reply loop must run on an isolated client and send no cookie.
    respx.get("https://api.bilibili.com/x/web-interface/view").mock(
        return_value=httpx.Response(200, json={"code": 0, "data": {"aid": 1, "stat": {"reply": 1}}})
    )
    respx.get("https://api.bilibili.com/x/web-interface/nav").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "wbi_img": {
                        "img_url": "https://i0.hdslb.com/bfs/wbi/abcdefghijklmnopqrstuvwxyzabcdef.png",
                        "sub_url": "https://i0.hdslb.com/bfs/wbi/ghijklmnopqrstuvwxyzabcdefghijkl.png",
                    }
                }
            },
        )
    )
    reply_route = respx.get("https://api.bilibili.com/x/v2/reply/wbi/main").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "cursor": {"is_end": True, "all_count": 1, "pagination_reply": {}},
                    "replies": [
                        {"rpid": 1, "member": {"uname": "a"}, "content": {"message": "hi"}}
                    ],
                },
            },
        )
    )

    jar = httpx.Cookies()
    jar.set("buvid3", "seeded-into-shared-jar", domain=".bilibili.com", path="/")
    async with httpx.AsyncClient(cookies=jar) as client:
        analyzer = CommentAnalyzer(Settings(), client)
        await analyzer.fetch_comment_page("https://www.bilibili.com/video/BV1BCGB66E8P/")

    assert reply_route.called
    for call in reply_route.calls:
        assert "cookie" not in {k.lower() for k in call.request.headers}


def test_select_top_comments_ranks_by_likes_then_replies() -> None:
    comments = [
        VideoComment(text="plain", like_count=9, reply_count=99),
        VideoComment(text="top", like_count=10, reply_count=1),
        VideoComment(text="also top", like_count=10, reply_count=3),
    ]

    top = select_top_comments(comments, count=2)

    assert [comment.text for comment in top] == ["also top", "top"]


def test_sort_comments_by_heat_orders_all_comments() -> None:
    comments = [
        VideoComment(text="cold", like_count=1, reply_count=99),
        VideoComment(text="hot", like_count=20, reply_count=0),
        VideoComment(text="warm", like_count=10, reply_count=5),
    ]

    sorted_comments = sort_comments_by_heat(comments)

    assert [comment.text for comment in sorted_comments] == ["hot", "warm", "cold"]


def test_comment_fetch_limit_caps_config_at_max() -> None:
    assert _comment_fetch_limit(Settings(comment_analysis_max_comments=600)) == 500
    assert _comment_fetch_limit(Settings(comment_analysis_max_comments=80)) == 80


def test_build_comment_analysis_prompt_requests_fixed_json_template() -> None:
    comments = [
        VideoComment(text="Great breakdown", author="Alice", like_count=10),
        VideoComment(text="I disagree", author="Bob", reply_count=2),
    ]

    prompt = build_comment_analysis_prompt(
        "https://youtu.be/abc",
        comments,
        comments[:1],
        comments,
        total_comment_count=88,
    )

    assert "评论总数: 88 条" in prompt
    assert "按热度" in prompt
    assert "翻译" in prompt
    assert "保持简洁" in prompt
    assert "避免多层 bullet" in prompt
    assert '"top_comment_translations"' in prompt
    assert '"sentiment"' in prompt
    assert '"best_comment"' not in prompt
    assert "严格 JSON" in prompt
    assert "Great breakdown" in prompt


def test_parse_comment_insight_accepts_json_code_fence() -> None:
    insight = parse_comment_insight(
        """```json
        {
          "summary": "大家认可视频观点",
          "consensus": ["观点清晰"],
          "controversy": ["细节有争议"],
          "notable_points": ["补充了背景"],
          "top_comment_translations": ["很有帮助"]
        }
        ```""",
        top_comment_count=3,
    )

    assert insight.summary == "大家认可视频观点"
    assert insight.consensus == ["观点清晰"]
    assert insight.top_comment_translations == ["很有帮助"]


def test_render_comment_analysis_markdown_uses_fixed_template() -> None:
    comments = [
        VideoComment(text="Great breakdown", author="Alice", like_count=10),
        VideoComment(text="I disagree", author="Bob", reply_count=2),
    ]
    insight = CommentInsight(
        summary="评论区整体认可视频观点。",
        sentiment="以正面称赞为主",
        consensus=["观点清晰", "例子有用"],
        controversy=["仍有人不同意结论"],
        notable_points=["补充了背景信息"],
        top_comment_translations=["很有帮助", "我不同意"],
    )

    markdown = render_comment_analysis_markdown(
        insight,
        comments,
        comments,
        prompt_count=2,
        total_comment_count=56,
    )

    assert "评论总数: 56 条" in markdown
    assert "样本 2 条" in markdown
    assert "情绪基调: 以正面称赞为主" in markdown
    assert "- **分析概览**" in markdown
    assert "- **最具信息量**" not in markdown
    assert "这条评论包含独家信息" not in markdown
    assert "- **热门评论**" in markdown
    assert "**Alice**" in markdown
    assert "> 很有帮助" in markdown
    assert "原文:" not in markdown


def _x_tweet(
    tweet_id: str,
    text: str,
    *,
    favorite_count: int = 0,
    reply_count: int = 0,
    parent_id: str | None = None,
) -> dict[str, object]:
    return {
        "__typename": "Tweet",
        "rest_id": tweet_id,
        "core": {
            "user_results": {
                "result": {
                    "core": {
                        "screen_name": "bob",
                        "name": "Bob",
                    },
                }
            }
        },
        "legacy": {
            "id_str": tweet_id,
            "conversation_id_str": "123",
            "full_text": text,
            "favorite_count": favorite_count,
            "reply_count": reply_count,
            "in_reply_to_status_id_str": parent_id,
        },
    }
