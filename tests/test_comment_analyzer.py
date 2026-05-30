import httpx
import respx

from src.comment_analyzer import (
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


def test_comment_fetch_limit_caps_config_at_200() -> None:
    assert _comment_fetch_limit(Settings(comment_analysis_max_comments=500)) == 200
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
    assert '"sentiment"' not in prompt
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
    assert "样本: 按热度抓取 2 条, 用于总结 2 条" in markdown
    assert "**评论区概览**" not in markdown
    assert "**简短结论**" not in markdown
    assert "**主要观察**" not in markdown
    assert "情绪" not in markdown
    assert "\n\n**评论翻译**\n" in markdown
    assert "1. **Alice** · 点赞 10 · 子评论 0" in markdown
    assert "> 很有帮助" in markdown
