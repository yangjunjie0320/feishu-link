import json

from src.card import (
    _fmt_count,
    _fmt_duration,
    _format_source_tag,
    build_card,
    build_markdown_card,
)
from src.parsers.base import LinkMetadata, MediaType


def test_fmt_duration_short() -> None:
    assert _fmt_duration(90) == "1:30"


def test_fmt_duration_long() -> None:
    assert _fmt_duration(3661) == "1:01:01"


def test_format_source_tag_youtube_is_red() -> None:
    assert _format_source_tag("YouTube") == "<font color='red'>[YouTube]</font>"


def test_format_source_tag_escapes_label() -> None:
    assert _format_source_tag("<Site>") == "<font color='grey'>[&lt;Site&gt;]</font>"


def test_fmt_count() -> None:
    assert _fmt_count(9999) == "9999"
    assert _fmt_count(12345) == "1.2万"
    assert _fmt_count(100000000) == "1亿"


def test_card_structure() -> None:
    meta = LinkMetadata(
        source_url="https://example.com",
        title="Example Article",
        description="A great read",
        cover_url="https://example.com/img.jpg",
        site_name="Example",
    )
    card = json.loads(build_card(meta, img_key="img_v2_xxx"))
    elements = card["elements"]
    media_row = elements[0]
    assert media_row["tag"] == "column_set"
    image_column = media_row["columns"][0]
    text_column = media_row["columns"][1]
    image = image_column["elements"][0]
    assert image["tag"] == "img"
    assert image["img_key"] == "img_v2_xxx"
    assert image["mode"] == "crop_center"
    assert image["compact_width"] is True
    assert image_column["weight"] == 1
    assert text_column["weight"] == 3
    body_text = text_column["elements"][0]["text"]["content"]
    assert "**Example Article**" in body_text
    assert "<font color='grey'>[Example]</font>" in body_text
    actions = [e for e in elements if e.get("tag") == "action"]
    assert actions
    assert actions[0]["actions"][0]["url"] == "https://example.com"
    assert actions[0]["actions"][0]["text"]["content"] == "打开链接"


def test_markdown_card_uses_markdown_component_and_normalizes_bullets() -> None:
    card = json.loads(
        build_markdown_card(
            "BibiGPT 总结",
            "## 重点 🔥\n* **第一点**\n  * 子点\n+ 第三点\n- 第二点",
            source_url="https://youtu.be/abc123",
        )
    )

    assert card["schema"] == "2.0"
    assert card["header"]["title"]["content"] == "BibiGPT 总结"
    content = card["body"]["elements"][0]["content"]
    assert card["body"]["elements"][0]["tag"] == "markdown"
    assert "- 重点" in content
    assert "- **第一点**" in content
    assert "    - 子点" in content
    assert "- 第三点" in content
    assert "🔥" not in content
    assert "## 重点" not in content
    assert "* **第一点**" not in content
    assert card["card_link"]["url"] == "https://youtu.be/abc123"
    link = card["body"]["elements"][1]
    assert link["tag"] == "markdown"
    assert link["content"] == "[打开视频](https://youtu.be/abc123)"


def test_markdown_card_preserves_nested_bullet_levels() -> None:
    card = json.loads(
        build_markdown_card(
            "BibiGPT 总结",
            "- **问题**\n"
            "  - **大学应如何调整课程？** ： 使用 AI 作为研究工具\n"
            "    - 保持第一性原理",
        )
    )

    content = card["body"]["elements"][0]["content"]
    assert content == (
        "- **问题**\n"
        "    - **大学应如何调整课程？** ： 使用 AI 作为研究工具\n"
        "        - 保持第一性原理"
    )


def test_markdown_card_can_collapse_content() -> None:
    card = json.loads(
        build_markdown_card(
            "BibiGPT 总结",
            "* 第一点\n+ 第二点",
            source_url="https://youtu.be/abc123",
            collapsed=True,
            panel_title="总结正文",
        )
    )

    panel = card["body"]["elements"][0]
    content = panel["elements"][0]["content"]
    assert panel["tag"] == "collapsible_panel"
    assert panel["expanded"] is False
    assert panel["header"]["title"]["content"] == "**总结正文**"
    assert panel["elements"][0]["tag"] == "markdown"
    assert "- 第一点" in content
    assert "- 第二点" in content
    link = card["body"]["elements"][1]
    assert link["content"] == "[打开视频](https://youtu.be/abc123)"


def test_card_no_cover() -> None:
    meta = LinkMetadata(
        source_url="https://example.com",
        title="No Cover",
    )
    card = json.loads(build_card(meta))
    tags = [e["tag"] for e in card["elements"]]
    assert "img" not in tags


def test_card_with_youtube_metadata() -> None:
    meta = LinkMetadata(
        source_url="https://youtu.be/abc123",
        title="Cool Video",
        site_name="YouTube",
        channel="Great Channel",
        media_type=MediaType.VIDEO,
        duration_seconds=305,
        view_count=12345,
        like_count=678,
        comment_count=90,
    )
    card = json.loads(build_card(meta))
    action_block = next(e for e in card["elements"] if e.get("tag") == "action")
    assert action_block["actions"][1]["text"]["content"] == "总结视频"
    assert action_block["actions"][1]["value"] == {
        "action": "summarize_video",
        "url": "https://youtu.be/abc123",
    }
    assert action_block["actions"][2]["text"]["content"] == "分析评论"
    assert action_block["actions"][2]["value"] == {
        "action": "analyze_comments",
        "url": "https://youtu.be/abc123",
    }
    assert action_block["actions"][3]["text"]["content"] == "下载视频"
    assert action_block["actions"][3]["value"] == {
        "action": "download_video",
        "url": "https://youtu.be/abc123",
    }
    body_text = next(
        e["text"]["content"] for e in card["elements"] if e.get("tag") == "div"
    )
    assert "5:05" in body_text
    assert "Great Channel" in body_text
    assert "<font color='red'>[YouTube]</font>" in body_text
    assert "播放 1.2万" in body_text
    assert "点赞 678" in body_text
    assert "评论 90" in body_text


def test_card_actions_prefer_canonical_url() -> None:
    meta = LinkMetadata(
        source_url="https://b23.tv/abc123",
        canonical_url="https://www.bilibili.com/video/BV1BCGB66E8P/",
        title="Bilibili Video",
        site_name="Bilibili",
        platform="bilibili",
        media_type=MediaType.VIDEO,
    )

    card = json.loads(build_card(meta))
    action_block = next(e for e in card["elements"] if e.get("tag") == "action")

    assert action_block["actions"][0]["url"] == "https://www.bilibili.com/video/BV1BCGB66E8P/"
    assert action_block["actions"][1]["value"]["url"] == "https://www.bilibili.com/video/BV1BCGB66E8P/"
    assert action_block["actions"][2]["value"]["url"] == "https://www.bilibili.com/video/BV1BCGB66E8P/"
    assert action_block["actions"][3]["value"]["url"] == "https://www.bilibili.com/video/BV1BCGB66E8P/"


def test_video_card_omits_summary_button_for_unsupported_platform() -> None:
    meta = LinkMetadata(
        source_url="https://example.com/video",
        title="Example Video",
        platform="example",
        media_type=MediaType.VIDEO,
    )

    card = json.loads(build_card(meta))
    action_block = next(e for e in card["elements"] if e.get("tag") == "action")
    labels = [action["text"]["content"] for action in action_block["actions"]]

    assert labels == ["打开链接", "下载视频"]


def test_instagram_article_card_includes_comment_analysis_button() -> None:
    meta = LinkMetadata(
        source_url="https://www.instagram.com/p/DYfWbunGlNg/",
        title="Post by beccu.studio",
        site_name="Instagram",
        platform="instagram",
        media_type=MediaType.ARTICLE,
    )

    card = json.loads(build_card(meta))
    action_block = next(e for e in card["elements"] if e.get("tag") == "action")
    labels = [action["text"]["content"] for action in action_block["actions"]]

    assert labels == ["打开链接", "分析评论"]
    assert action_block["actions"][1]["value"] == {
        "action": "analyze_comments",
        "url": "https://www.instagram.com/p/DYfWbunGlNg/",
    }


def test_video_card_omits_description_even_if_translated() -> None:
    meta = LinkMetadata(
        source_url="https://youtu.be/abc123",
        title="Useful Video Title",
        description="Get 30% off today. Long sponsor copy follows.",
        translated_description="今日购买可享 30% 折扣。后面是很长的赞助文案。",
        site_name="YouTube",
        platform="youtube",
        media_type=MediaType.VIDEO,
    )
    card = json.loads(build_card(meta))
    body_text = next(
        e["text"]["content"] for e in card["elements"] if e.get("tag") == "div"
    )
    assert "Useful Video Title" in body_text
    assert "30%" not in body_text
    assert "赞助文案" not in body_text


def test_card_with_parse_warning() -> None:
    meta = LinkMetadata(
        source_url="https://www.instagram.com/reel/abc/",
        title="Instagram Reel",
        site_name="Instagram",
        parse_warnings=["instagram 内容受限或需要 cookie, 已先发送卡片"],
    )
    card = json.loads(build_card(meta))
    body_text = next(
        e["text"]["content"] for e in card["elements"] if e.get("tag") == "div"
    )
    assert "需要 cookie" in body_text


def test_card_with_translated_title_keeps_original() -> None:
    meta = LinkMetadata(
        source_url="https://www.tiktok.com/@u/video/123",
        title="Bro we boutta get some super villains",
        translated_title="兄弟, 我们快要造出超级反派了",
        site_name="TikTok",
    )
    card = json.loads(build_card(meta))
    body_text = next(
        e["text"]["content"] for e in card["elements"] if e.get("tag") == "div"
    )
    assert "**兄弟, 我们快要造出超级反派了**" in body_text
    assert "原标题: Bro we boutta get some super villains" in body_text


def test_card_with_translated_description_keeps_original() -> None:
    meta = LinkMetadata(
        source_url="https://www.instagram.com/p/abc/",
        title="Post by beccu.studio",
        description="BMW M1 widebody, covered in custom rhinestone artwork.",
        translated_description="BMW M1 宽体车, 覆盖定制水钻艺术装饰。",
        site_name="Instagram",
        platform="instagram",
    )
    card = json.loads(build_card(meta))
    body_text = next(
        e["text"]["content"] for e in card["elements"] if e.get("tag") == "div"
    )
    assert "BMW M1 宽体车" in body_text
    assert "原文: BMW M1 widebody" in body_text
    assert "Post by beccu.studio" not in body_text
    assert "原标题" not in body_text


def test_article_description_removes_newlines_and_emoji() -> None:
    meta = LinkMetadata(
        source_url="https://www.instagram.com/p/abc/",
        title="Post by al.yasid",
        description="| Eh, Mamma Mia 🤌🏽\n\nA slightly modded Ferrari F40.",
        translated_description="嗯, 妈妈咪呀 🤌🏽\n五年前我轻度改装过一辆法拉利 F40。",
        site_name="Instagram",
        platform="instagram",
        media_type=MediaType.ARTICLE,
    )
    card = json.loads(build_card(meta))
    body_text = next(
        e["text"]["content"] for e in card["elements"] if e.get("tag") == "div"
    )

    assert "🤌" not in body_text
    assert "🏽" not in body_text
    assert "妈妈咪呀 五年前" in body_text
    assert "Mamma Mia A slightly" in body_text
    assert "Mamma Mia\n\nA slightly" not in body_text
    assert "F40。\n<font color='grey'>原文:" not in body_text


def test_article_description_without_translation_removes_newlines_and_emoji() -> None:
    meta = LinkMetadata(
        source_url="https://www.instagram.com/p/abc/",
        title="Post by al.yasid",
        description="First line ✌🏼\nsecond line",
        site_name="Instagram",
        platform="instagram",
        media_type=MediaType.ARTICLE,
    )
    card = json.loads(build_card(meta))
    body_text = next(
        e["text"]["content"] for e in card["elements"] if e.get("tag") == "div"
    )

    assert "✌" not in body_text
    assert "🏼" not in body_text
    assert "First line second line" in body_text


def test_social_image_card_does_not_show_technical_warning() -> None:
    meta = LinkMetadata(
        source_url="https://www.instagram.com/p/abc/",
        title="Post by beccu.studio",
        description="Get in, loser. We're going shopping.",
        translated_description="上车吧, 失败者。我们去购物。",
        site_name="Instagram",
        platform="instagram",
        parse_warnings=[],
    )
    card = json.loads(build_card(meta))
    body_text = next(
        e["text"]["content"] for e in card["elements"] if e.get("tag") == "div"
    )
    assert "未尝试下载视频" not in body_text
    assert "图文内容已发送卡片" not in body_text


def test_other_social_article_card_uses_description_as_primary() -> None:
    meta = LinkMetadata(
        source_url="https://www.tiktok.com/@u/photo/123",
        title="TikTok Post",
        description="A tiny desk setup with three vintage monitors.",
        translated_description="一个摆着三台复古显示器的小桌面布置。",
        site_name="TikTok",
        platform="tiktok",
    )
    card = json.loads(build_card(meta))
    body_text = next(
        e["text"]["content"] for e in card["elements"] if e.get("tag") == "div"
    )
    assert body_text.startswith("一个摆着三台复古显示器的小桌面布置。")
    assert "原文: A tiny desk setup" in body_text
    assert "TikTok Post" not in body_text


def test_web_article_card_keeps_title_primary() -> None:
    meta = LinkMetadata(
        source_url="https://example.com/post",
        title="Article Title",
        description="A short article summary.",
        translated_description="一段短文章摘要。",
        site_name="Example",
        platform="web",
    )
    card = json.loads(build_card(meta))
    body_text = next(
        e["text"]["content"] for e in card["elements"] if e.get("tag") == "div"
    )
    assert body_text.startswith("**Article Title**")
    assert "一段短文章摘要。" in body_text


def test_zhihu_article_card_uses_description_as_primary() -> None:
    meta = LinkMetadata(
        source_url="https://www.zhihu.com/question/123/answer/456",
        title="知乎回答标题",
        description="这是知乎回答摘要。",
        site_name="知乎",
        platform="zhihu",
    )
    card = json.loads(build_card(meta))
    body_text = next(
        e["text"]["content"] for e in card["elements"] if e.get("tag") == "div"
    )
    assert body_text.startswith("<font color='grey'>这是知乎回答摘要。</font>")
    assert "知乎回答标题" not in body_text
