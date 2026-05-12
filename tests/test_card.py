import json

from feishu_link.card import _fmt_duration, build_card
from feishu_link.parsers.base import LinkMetadata


def test_fmt_duration_short() -> None:
    assert _fmt_duration(90) == "1:30"


def test_fmt_duration_long() -> None:
    assert _fmt_duration(3661) == "1:01:01"


def test_card_structure() -> None:
    meta = LinkMetadata(
        source_url="https://example.com",
        title="Example Article",
        description="A great read",
        cover_url="https://example.com/img.jpg",
        site_name="Example",
    )
    card = json.loads(build_card(meta))
    assert card["header"]["title"]["content"] == "Example Article"
    elements = card["elements"]
    # first element is cover image
    assert elements[0]["tag"] == "img"
    # action button
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
    )
    card = json.loads(build_card(meta))
    body_text = next(
        e["text"]["content"] for e in card["elements"] if e.get("tag") == "div"
    )
    assert "5:05" in body_text
    assert "Great Channel" in body_text
