import json
import unicodedata

from src.bibi_models import ChapterSummarySection
from src.card import (
    _fmt_count,
    _format_source_tag,
    _split_markdown,
    build_card,
    build_chapter_summary_cards,
    build_markdown_card,
    card_message_wire_size,
    fmt_duration,
)
from src.parsers.base import LinkMetadata, MediaType


def test_fmt_duration_short() -> None:
    assert fmt_duration(90) == "1:30"


def test_fmt_duration_long() -> None:
    assert fmt_duration(3661) == "1:01:01"


def test_format_source_tag_youtube_is_red() -> None:
    assert _format_source_tag("YouTube") == "<font color='red'>[YouTube]</font>"


def test_format_source_tag_escapes_label() -> None:
    assert _format_source_tag("<Site>") == "<font color='grey'>[&lt;Site&gt;]</font>"


def test_fmt_count() -> None:
    assert _fmt_count(9999) == "9999"
    assert _fmt_count(12345) == "1.2万"
    assert _fmt_count(100000000) == "1亿"


def test_chapter_summary_card_renders_introduction_and_timeline_section() -> None:
    cards = build_chapter_summary_cards(
        "这是视频的时间线总述。",
        (
            ChapterSummarySection(
                index=0,
                start_time=4,
                end_time=8,
                title="项目背景",
                summary="介绍项目的目标和设计思路。",
            ),
        ),
    )

    assert len(cards) == 1
    card = json.loads(cards[0])
    assert card["header"]["title"]["content"] == "BibiGPT 字幕总结"
    panels = card["body"]["elements"]
    assert len(panels) == 1
    assert panels[0]["tag"] == "collapsible_panel"
    assert panels[0]["expanded"] is False
    assert panels[0]["header"]["title"]["content"] == "**字幕总结**"
    assert panels[0]["elements"] == [
        {
            "tag": "markdown",
            "content": (
                "这是视频的时间线总述。\n\n**[00:04–00:08] 项目背景**\n介绍项目的目标和设计思路。"
            ),
        }
    ]
    assert "card_link" not in card
    assert "打开视频" not in cards[0]


def test_chapter_summary_card_strips_emoji_from_all_fields() -> None:
    cards = build_chapter_summary_cards(
        "🔥 这是视频的时间线总述。✨",
        (
            ChapterSummarySection(
                index=0,
                start_time=4,
                end_time=8,
                title="🚀 项目背景 1️⃣",
                summary="介绍项目的目标和设计思路。👍🏻",
            ),
            ChapterSummarySection(
                index=1,
                start_time=8,
                end_time=12,
                title="💯",
                summary="🎉🎉",
            ),
        ),
    )

    assert len(cards) == 1
    content = json.loads(cards[0])["body"]["elements"][0]["elements"][0]["content"]
    assert "这是视频的时间线总述。" in content
    assert "**[00:04–00:08] 项目背景 1**" in content
    assert "介绍项目的目标和设计思路。" in content
    assert "**[00:08–00:12] 章节**" in content
    assert "（无内容）" in content
    assert not any(ord(ch) > 0x1F000 for ch in content)
    assert "✨" not in content and "1️⃣"[1:] not in content


def test_chapter_summary_card_renders_hour_timestamp() -> None:
    cards = build_chapter_summary_cards(
        "",
        [
            ChapterSummarySection(
                index=7,
                start_time=3661.9,
                end_time=3723.2,
                title="后续计划",
                summary="说明项目下一步工作。",
            )
        ],
    )

    content = json.loads(cards[0])["body"]["elements"][0]["elements"][0]["content"]
    assert content == "**[1:01:01–1:02:03] 后续计划**\n说明项目下一步工作。"


def test_chapter_summary_cards_split_chinese_content_at_section_boundaries() -> None:
    sections = [
        ChapterSummarySection(
            index=index,
            start_time=index * 5,
            end_time=index * 5 + 5,
            title=f"章节 {index}",
            summary=f"第{index}段" + "中" * 5_000,
        )
        for index in range(3)
    ]

    cards = build_chapter_summary_cards("总述只应出现一次。", sections)

    assert len(cards) == 3
    contents: list[str] = []
    for index, card_json in enumerate(cards, start=1):
        card = json.loads(card_json)
        panel = card["body"]["elements"][0]
        assert len(card["body"]["elements"]) == 1
        assert panel["header"]["title"]["content"] == f"**字幕总结（{index}/3）**"
        assert "（续）" not in panel["elements"][0]["content"]
        contents.append(panel["elements"][0]["content"])
        assert card_message_wire_size(card_json) <= 24 * 1024
        assert card_message_wire_size(card_json) < 30 * 1024

    assert sum(content.count("总述只应出现一次。") for content in contents) == 1
    for index, content in enumerate(contents):
        assert f"章节 {index}" in content


def test_chapter_summary_cards_split_oversized_section_without_losing_unicode() -> None:
    original = "汉𠮷é" * 8_000
    cards = build_chapter_summary_cards(
        "",
        [
            ChapterSummarySection(
                index=0,
                start_time=0,
                end_time=1,
                title="超长章节",
                summary=original,
            )
        ],
    )

    assert len(cards) > 1
    prefix = "**[00:00–00:01] 超长章节**\n"
    reconstructed: list[str] = []
    for index, card_json in enumerate(cards):
        panel = json.loads(card_json)["body"]["elements"][0]
        content = panel["elements"][0]["content"]
        assert content.startswith(prefix)
        fragment = content[len(prefix) :]
        if index:
            assert fragment.startswith("（续）")
            fragment = fragment.removeprefix("（续）")
        else:
            assert not fragment.startswith("（续）")
        assert not fragment or unicodedata.combining(fragment[0]) == 0
        reconstructed.append(fragment)
        assert card_message_wire_size(card_json) <= 24 * 1024

    assert "".join(reconstructed) == original


def test_chapter_summary_cards_do_not_split_combining_mark_grapheme() -> None:
    grapheme = "é"
    original = grapheme * 12_000
    cards = build_chapter_summary_cards(
        "",
        [
            ChapterSummarySection(
                index=0,
                start_time=0,
                end_time=1,
                title="超长章节",
                summary=original,
            )
        ],
    )

    prefix = "**[00:00–00:01] 超长章节**\n"
    reconstructed: list[str] = []
    for index, card_json in enumerate(cards):
        content = json.loads(card_json)["body"]["elements"][0]["elements"][0]["content"]
        fragment = content.removeprefix(prefix)
        if index:
            fragment = fragment.removeprefix("（续）")
        assert not fragment.startswith("\u0301")
        reconstructed.append(fragment)

    assert len(cards) > 1
    assert "".join(reconstructed) == original


def test_chapter_summary_cards_split_oversized_title_without_losing_text() -> None:
    title = "章" * 10_000
    summary = "短摘要"
    cards = build_chapter_summary_cards(
        "",
        [
            ChapterSummarySection(
                index=0,
                start_time=0,
                end_time=1,
                title=title,
                summary=summary,
            )
        ],
    )

    assert len(cards) > 1
    contents = [
        json.loads(card_json)["body"]["elements"][0]["elements"][0]["content"]
        for card_json in cards
    ]
    assert sum(content.count("章") for content in contents) == len(title)
    assert sum(content.count(summary) for content in contents) == 1
    assert any("（标题续）" in content for content in contents[1:])
    assert all(card_message_wire_size(card_json) <= 24 * 1024 for card_json in cards)


def test_chapter_summary_cards_require_at_least_one_section() -> None:
    assert build_chapter_summary_cards("只有总述", ()) == []


def test_card_message_wire_size_covers_reply_and_archive_payloads() -> None:
    card_json = json.dumps({"text": "中文"}, ensure_ascii=False, separators=(",", ":"))
    reply_payload = json.dumps(
        {"content": card_json, "msg_type": "interactive"},
        ensure_ascii=False,
    )
    archive_payload = json.dumps(
        {
            "receive_id": "oc_real_archive_chat_id",
            "msg_type": "interactive",
            "content": card_json,
        },
        ensure_ascii=False,
    )

    assert card_message_wire_size(card_json) >= len(reply_payload.encode("utf-8"))
    assert card_message_wire_size(card_json) >= len(archive_payload.encode("utf-8"))


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


def test_split_markdown_extracts_preamble() -> None:
    markdown = "`#AI` `#技术`\n\n- **总结**\n    - 一句话\n- **亮点**\n    - 亮点1"
    preamble, sections = _split_markdown(markdown)

    assert "`#AI`" in preamble
    assert "`#技术`" in preamble
    assert len(sections) == 2
    assert sections[0][0] == "总结"


def test_build_markdown_card_sectioned_renders_preamble_inline() -> None:
    markdown = "`#AI` `#技术`\n\n- **总结**\n    - 内容\n- **亮点**\n    - 亮点1"
    card = json.loads(build_markdown_card("Title", markdown, collapsed=True, sectioned=True))
    elements = card["body"]["elements"]

    assert elements[0]["tag"] == "markdown"
    assert "`#AI`" in elements[0]["content"]
    panels = [el for el in elements if el.get("tag") == "collapsible_panel"]
    assert len(panels) == 2


def test_split_markdown_returns_sections() -> None:
    markdown = "- **总结**\n    - 点1\n    - 点2\n- **亮点**\n    - 亮1\n- **问题**\n    - Q1"
    _, sections = _split_markdown(markdown)

    assert len(sections) == 3
    assert sections[0][0] == "总结"
    assert "点1" in sections[0][1]
    assert "点2" in sections[0][1]
    assert sections[1][0] == "亮点"
    assert sections[2][0] == "问题"


def test_build_markdown_card_sectioned_creates_per_section_panels() -> None:
    markdown = "- **总结**\n    - 一句话\n- **亮点**\n    - 亮点1\n- **问题**\n    - 问题1"
    card = json.loads(
        build_markdown_card(
            "BibiGPT 总结",
            markdown,
            source_url="https://youtu.be/abc",
            collapsed=True,
            sectioned=True,
        )
    )
    elements = card["body"]["elements"]
    panels = [el for el in elements if el.get("tag") == "collapsible_panel"]

    assert len(panels) == 3
    assert panels[0]["header"]["title"]["content"] == "**总结**"
    assert panels[1]["header"]["title"]["content"] == "**亮点**"
    assert panels[2]["header"]["title"]["content"] == "**问题**"
    assert all(panel["expanded"] is False for panel in panels)


def test_build_markdown_card_sectioned_falls_back_when_single_section() -> None:
    markdown = "- 只有一节\n    - 内容"
    card = json.loads(
        build_markdown_card(
            "Title",
            markdown,
            collapsed=True,
            panel_title="正文",
            sectioned=True,
        )
    )
    panels = [el for el in card["body"]["elements"] if el.get("tag") == "collapsible_panel"]

    assert len(panels) == 1
    assert panels[0]["header"]["title"]["content"] == "**正文**"


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
    body_text = next(e["text"]["content"] for e in card["elements"] if e.get("tag") == "div")
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
    assert (
        action_block["actions"][1]["value"]["url"] == "https://www.bilibili.com/video/BV1BCGB66E8P/"
    )
    assert (
        action_block["actions"][2]["value"]["url"] == "https://www.bilibili.com/video/BV1BCGB66E8P/"
    )
    assert (
        action_block["actions"][3]["value"]["url"] == "https://www.bilibili.com/video/BV1BCGB66E8P/"
    )


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
    body_text = next(e["text"]["content"] for e in card["elements"] if e.get("tag") == "div")
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
    body_text = next(e["text"]["content"] for e in card["elements"] if e.get("tag") == "div")
    assert "需要 cookie" in body_text


def test_card_with_translated_title_keeps_original() -> None:
    meta = LinkMetadata(
        source_url="https://www.tiktok.com/@u/video/123",
        title="Bro we boutta get some super villains",
        translated_title="兄弟, 我们快要造出超级反派了",
        site_name="TikTok",
    )
    card = json.loads(build_card(meta))
    body_text = next(e["text"]["content"] for e in card["elements"] if e.get("tag") == "div")
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
    body_text = next(e["text"]["content"] for e in card["elements"] if e.get("tag") == "div")
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
    body_text = next(e["text"]["content"] for e in card["elements"] if e.get("tag") == "div")

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
    body_text = next(e["text"]["content"] for e in card["elements"] if e.get("tag") == "div")

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
    body_text = next(e["text"]["content"] for e in card["elements"] if e.get("tag") == "div")
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
    body_text = next(e["text"]["content"] for e in card["elements"] if e.get("tag") == "div")
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
    body_text = next(e["text"]["content"] for e in card["elements"] if e.get("tag") == "div")
    assert body_text.startswith("**Article Title**")
    assert "一段短文章摘要。" in body_text


def test_instagram_article_card_uses_description_as_primary() -> None:
    meta = LinkMetadata(
        source_url="https://www.instagram.com/p/abc123/",
        title="Post by someuser",
        description="这是 Instagram 图文帖正文。",
        site_name="Instagram",
        platform="instagram",
        media_type=MediaType.ARTICLE,
    )
    card = json.loads(build_card(meta))
    body_text = next(e["text"]["content"] for e in card["elements"] if e.get("tag") == "div")
    assert body_text.startswith("<font color='grey'>这是 Instagram 图文帖正文。</font>")
    assert "Post by someuser" not in body_text
