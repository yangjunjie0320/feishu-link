import json

from feishu_link.card import _fmt_count, _fmt_duration, _format_source_tag, build_card
from feishu_link.parsers.base import LinkMetadata


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
        duration_seconds=305,
        view_count=12345,
        like_count=678,
        comment_count=90,
    )
    card = json.loads(build_card(meta))
    body_text = next(
        e["text"]["content"] for e in card["elements"] if e.get("tag") == "div"
    )
    assert "5:05" in body_text
    assert "Great Channel" in body_text
    assert "<font color='red'>[YouTube]</font>" in body_text
    assert "播放 1.2万" in body_text
    assert "点赞 678" in body_text
    assert "评论 90" in body_text


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
    )
    card = json.loads(build_card(meta))
    body_text = next(
        e["text"]["content"] for e in card["elements"] if e.get("tag") == "div"
    )
    assert "BMW M1 宽体车" in body_text
    assert "原文: BMW M1 widebody" in body_text
