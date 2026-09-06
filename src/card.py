from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import replace
from html import escape

from .bibi_models import ChapterSummarySection
from .parsers.base import LinkMetadata, MediaType

_SOURCE_COLORS = {
    "youtube": "red",
    "github": "grey",
    "twitter": "blue",
    "x": "blue",
    "bilibili": "blue",
    "instagram": "purple",
    "tiktok": "grey",
}

_CHAPTER_SUMMARY_CARD_TARGET_BYTES = 24 * 1024
_CHAPTER_SUMMARY_CARD_HARD_LIMIT_BYTES = 30 * 1024
_FEISHU_RECEIVE_ID_SIZE_BUDGET = 256


def fmt_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _truncate(text: str, max_chars: int) -> str:
    return text[:max_chars].rstrip() + "..." if len(text) > max_chars else text


def _format_source_tag(label: str) -> str:
    color = _SOURCE_COLORS.get(label.strip().lower(), "grey")
    safe_label = escape(label.strip())
    return f"<font color='{color}'>[{safe_label}]</font>"


def _fmt_count(value: int) -> str:
    if value >= 100_000_000:
        compact = value / 100_000_000
        return f"{compact:.1f}".rstrip("0").rstrip(".") + "亿"
    if value >= 10_000:
        compact = value / 10_000
        return f"{compact:.1f}".rstrip("0").rstrip(".") + "万"
    return str(value)


def _format_stats(meta: LinkMetadata) -> str:
    parts: list[str] = []
    if meta.view_count is not None:
        parts.append(f"播放 {_fmt_count(meta.view_count)}")
    if meta.like_count is not None:
        parts.append(f"点赞 {_fmt_count(meta.like_count)}")
    if meta.comment_count is not None:
        parts.append(f"评论 {_fmt_count(meta.comment_count)}")
    if meta.repost_count is not None:
        parts.append(f"转发 {_fmt_count(meta.repost_count)}")
    return " · ".join(parts)


def build_markdown_card(
    title: str,
    markdown: str,
    *,
    source_url: str | None = None,
    collapsed: bool = False,
    panel_title: str = "正文",
    sectioned: bool = False,
    extra_links: Sequence[tuple[str, str]] = (),
) -> str:
    safe_title = _strip_emoji_symbols(title).strip() or "Summary"
    content = _normalize_summary_markdown(markdown)
    if collapsed:
        preamble, sections = _split_markdown(content) if sectioned else ("", [])
        if len(sections) >= 2:
            elements: list[dict[str, object]] = []
            if preamble:
                elements.append({"tag": "markdown", "content": preamble})
            elements.extend(
                _build_section_panel(sec_title, sec_body)
                for sec_title, sec_body in sections
                if _strip_emoji_symbols(sec_title).strip() and _section_has_content(sec_body)
            )
        else:
            safe_panel_title = _strip_emoji_symbols(panel_title).strip() or "正文"
            elements = [
                {
                    "tag": "collapsible_panel",
                    "expanded": False,
                    "header": {
                        "title": {
                            "tag": "markdown",
                            "content": f"**{safe_panel_title}**",
                        },
                        "vertical_align": "center",
                        "padding": "4px 0px 4px 8px",
                    },
                    "elements": [{"tag": "markdown", "content": content}],
                }
            ]
    else:
        elements = [{"tag": "markdown", "content": content}]

    links = ([("打开视频", source_url)] if source_url else []) + [
        (label, url) for label, url in extra_links if label and url
    ]
    if links:
        elements.append(
            {
                "tag": "markdown",
                "content": " · ".join(f"[{label}]({url})" for label, url in links),
            }
        )

    card: dict[str, object] = {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": safe_title},
            "template": "blue",
        },
        "body": {"elements": elements},
    }
    if source_url:
        card["card_link"] = {"url": source_url}

    return json.dumps(card, ensure_ascii=False)


def card_message_wire_size(card_json: str) -> int:
    """Return a conservative UTF-8 size for either Feishu send mode."""
    reply_payload = {"content": card_json, "msg_type": "interactive"}
    archive_payload = {
        "receive_id": "x" * _FEISHU_RECEIVE_ID_SIZE_BUDGET,
        "msg_type": "interactive",
        "content": card_json,
    }
    return max(
        len(json.dumps(reply_payload, ensure_ascii=False).encode("utf-8")),
        len(json.dumps(archive_payload, ensure_ascii=False).encode("utf-8")),
    )


def build_chapter_summary_cards(
    introduction: str,
    sections: Sequence[ChapterSummarySection],
) -> list[str]:
    """Render BibiGPT's timeline summary into size-safe Feishu cards."""
    if not sections:
        return []

    introduction = _strip_emoji_symbols(introduction)
    sections = [
        replace(
            section,
            title=_strip_emoji_symbols(section.title).strip() or "章节",
            summary=_strip_emoji_symbols(section.summary).strip() or "（无内容）",
        )
        for section in sections
    ]

    content_units = [
        *([introduction.strip()] if introduction.strip() else []),
        *(_format_chapter_summary_section(section) for section in sections),
    ]
    single_content = "\n\n".join(content_units)
    single_card = _build_chapter_summary_card(single_content, panel_title="字幕总结")
    if card_message_wire_size(single_card) <= _CHAPTER_SUMMARY_CARD_TARGET_BYTES:
        return [single_card]

    sizing_title = "字幕总结（1/1）"
    while True:
        contents = _partition_chapter_summary_contents(
            introduction,
            sections,
            panel_title=sizing_title,
        )
        total = len(contents)
        next_sizing_title = f"字幕总结（{total}/{total}）"
        if len(next_sizing_title.encode("utf-8")) == len(sizing_title.encode("utf-8")):
            break
        sizing_title = next_sizing_title

    cards = [
        _build_chapter_summary_card(
            content,
            panel_title=f"字幕总结（{index}/{total}）",
        )
        for index, content in enumerate(contents, start=1)
    ]
    if any(
        card_message_wire_size(card) >= _CHAPTER_SUMMARY_CARD_HARD_LIMIT_BYTES for card in cards
    ):
        raise ValueError("chapter summary card exceeds Feishu's hard message size limit")
    return cards


def _partition_chapter_summary_contents(
    introduction: str,
    sections: Sequence[ChapterSummarySection],
    *,
    panel_title: str,
) -> list[str]:
    units: list[str] = []
    normalized_introduction = introduction.strip()
    if normalized_introduction:
        if _chapter_summary_content_fits(
            normalized_introduction,
            panel_title=panel_title,
        ):
            units.append(normalized_introduction)
        else:
            units.extend(
                _split_oversized_chapter_text(
                    normalized_introduction,
                    first_prefix="",
                    continuation_prefix="（续）",
                    panel_title=panel_title,
                )
            )

    for section in sections:
        rendered = _format_chapter_summary_section(section)
        if _chapter_summary_content_fits(rendered, panel_title=panel_title):
            units.append(rendered)
        else:
            units.extend(
                _split_oversized_chapter_section(
                    section,
                    panel_title=panel_title,
                )
            )

    contents: list[str] = []
    current: list[str] = []
    for unit in units:
        candidate = "\n\n".join([*current, unit])
        if current and not _chapter_summary_content_fits(
            candidate,
            panel_title=panel_title,
        ):
            contents.append("\n\n".join(current))
            current = [unit]
        else:
            current.append(unit)

    if current:
        contents.append("\n\n".join(current))
    return contents


def _split_oversized_chapter_section(
    section: ChapterSummarySection,
    *,
    panel_title: str,
) -> list[str]:
    prefix = _chapter_summary_section_prefix(section)
    if not _chapter_summary_content_fits(prefix, panel_title=panel_title):
        return _split_oversized_chapter_title_and_summary(
            section,
            panel_title=panel_title,
        )
    return _split_oversized_chapter_text(
        section.summary,
        first_prefix=prefix,
        continuation_prefix=f"{prefix}（续）",
        panel_title=panel_title,
    )


def _split_oversized_chapter_title_and_summary(
    section: ChapterSummarySection,
    *,
    panel_title: str,
) -> list[str]:
    start = _fmt_chapter_summary_timestamp(section.start_time)
    end = _fmt_chapter_summary_timestamp(section.end_time)
    timeline_prefix = f"**[{start}–{end}] "
    title_fragments = _split_oversized_chapter_text(
        section.title,
        first_prefix=timeline_prefix,
        continuation_prefix=f"{timeline_prefix}（标题续）",
        suffix="**\n",
        panel_title=panel_title,
    )
    summary_fragments = _split_oversized_chapter_text(
        section.summary,
        first_prefix=f"{timeline_prefix}（摘要）**\n",
        continuation_prefix=f"{timeline_prefix}（摘要续）**\n",
        panel_title=panel_title,
    )
    return [*title_fragments, *summary_fragments]


def _split_oversized_chapter_text(
    text: str,
    *,
    first_prefix: str,
    continuation_prefix: str,
    suffix: str = "",
    panel_title: str,
) -> list[str]:
    remaining = text
    fragments: list[str] = []
    first = True

    while remaining:
        prefix = first_prefix if first else continuation_prefix

        def render(
            fragment: str,
            current_prefix: str = prefix,
            current_suffix: str = suffix,
        ) -> str:
            return f"{current_prefix}{fragment}{current_suffix}"

        fragment_length = _largest_fitting_prefix(
            remaining,
            render=render,
            panel_title=panel_title,
        )
        if fragment_length == 0:
            raise ValueError("chapter summary card metadata leaves no room for content")
        fragments.append(render(remaining[:fragment_length]))
        remaining = remaining[fragment_length:]
        first = False

    if not fragments:
        fragments.append(f"{first_prefix}{suffix}")
    return fragments


def _largest_fitting_prefix(
    text: str,
    *,
    render: Callable[[str], str],
    panel_title: str,
) -> int:
    low = 1
    high = len(text)
    best = 0
    while low <= high:
        middle = (low + high) // 2
        candidate = render(text[:middle])
        if _chapter_summary_content_fits(candidate, panel_title=panel_title):
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    if best == 0 or best == len(text):
        return best
    safe_boundary = _previous_unicode_text_boundary(text, best)
    return safe_boundary or best


def _previous_unicode_text_boundary(text: str, boundary: int) -> int:
    """Move a split point left so the next fragment does not start mid-grapheme."""
    candidate = min(max(boundary, 0), len(text))
    while 0 < candidate < len(text):
        left = text[candidate - 1]
        right = text[candidate]
        if _is_safe_unicode_text_boundary(text, candidate, left, right):
            break
        candidate -= 1
    return candidate


def _is_safe_unicode_text_boundary(
    text: str,
    boundary: int,
    left: str,
    right: str,
) -> bool:
    if left == "\r" and right == "\n":
        return False
    if left == "\u200d" or right == "\u200d":
        return False
    if _is_unicode_grapheme_extension(right):
        return False
    if _is_regional_indicator(left) and _is_regional_indicator(right):
        preceding_indicators = 0
        position = boundary - 1
        while position >= 0 and _is_regional_indicator(text[position]):
            preceding_indicators += 1
            position -= 1
        return preceding_indicators % 2 == 0
    return True


def _is_unicode_grapheme_extension(character: str) -> bool:
    codepoint = ord(character)
    return (
        unicodedata.category(character) in {"Mc", "Me", "Mn"}
        or 0x1F3FB <= codepoint <= 0x1F3FF
        or 0xE0020 <= codepoint <= 0xE007F
    )


def _is_regional_indicator(character: str) -> bool:
    return 0x1F1E6 <= ord(character) <= 0x1F1FF


def _chapter_summary_content_fits(content: str, *, panel_title: str) -> bool:
    card = _build_chapter_summary_card(content, panel_title=panel_title)
    return card_message_wire_size(card) <= _CHAPTER_SUMMARY_CARD_TARGET_BYTES


def _format_chapter_summary_section(section: ChapterSummarySection) -> str:
    return f"{_chapter_summary_section_prefix(section)}{section.summary}"


def _chapter_summary_section_prefix(section: ChapterSummarySection) -> str:
    start = _fmt_chapter_summary_timestamp(section.start_time)
    end = _fmt_chapter_summary_timestamp(section.end_time)
    return f"**[{start}–{end}] {section.title}**\n"


def _fmt_chapter_summary_timestamp(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _build_chapter_summary_card(content: str, *, panel_title: str) -> str:
    card: dict[str, object] = {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "BibiGPT 字幕总结"},
            "template": "blue",
        },
        "body": {
            "elements": [
                {
                    "tag": "collapsible_panel",
                    "expanded": False,
                    "header": {
                        "title": {"tag": "markdown", "content": f"**{panel_title}**"},
                        "vertical_align": "center",
                        "padding": "4px 0px 4px 8px",
                    },
                    "elements": [{"tag": "markdown", "content": content}],
                }
            ]
        },
    }
    return json.dumps(card, ensure_ascii=False, separators=(",", ":"))


def build_card(meta: LinkMetadata, img_key: str | None = None) -> str:
    elements: list[dict[str, object]] = []

    meta_parts: list[str] = []
    source_label = meta.site_name or meta.platform
    if source_label:
        meta_parts.append(source_label)
    if meta.channel:
        meta_parts.append(meta.channel)
    if meta.duration_seconds is not None:
        meta_parts.append(fmt_duration(meta.duration_seconds))

    description_block = _format_description_block(meta)
    if _description_should_be_primary(meta, description_block):
        body = description_block
    else:
        raw_title = meta.translated_title or meta.title
        title = _truncate(raw_title, 60) if raw_title else (meta.site_name or "Link")
        body = f"**{title}**"
        if meta.translated_title and meta.title:
            body += f"\n<font color='grey'>原标题: {escape(_truncate(meta.title, 80))}</font>"
    if meta_parts:
        tag = _format_source_tag(meta_parts[0])
        body += f"\n{tag}"
        if len(meta_parts) > 1:
            body += f" · {' · '.join(meta_parts[1:])}"
    stats = _format_stats(meta)
    if stats:
        body += f"\n<font color='grey'>{escape(stats)}</font>"
    if description_block and not _description_should_be_primary(meta, description_block):
        body += f"\n{description_block}"
    if meta.parse_warnings:
        body += f"\n<font color='grey'>{escape(_truncate(meta.parse_warnings[0], 80))}</font>"

    text_element = {
        "tag": "div",
        "text": {"tag": "lark_md", "content": body},
    }

    if img_key:
        elements.append(_build_compact_media_row(img_key, text_element))
    else:
        elements.append(text_element)

    actions = _build_actions(meta)
    if actions:
        elements.append({"tag": "action", "actions": actions})

    card: dict[str, object] = {
        "config": {"wide_screen_mode": True},
        "elements": elements,
    }

    return json.dumps(card, ensure_ascii=False)


def _build_actions(meta: LinkMetadata) -> list[dict[str, object]]:
    action_url = _action_url(meta)
    actions: list[dict[str, object]] = []

    if meta.media_type == MediaType.VIDEO and _supports_summary_action(meta):
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "总结视频"},
                "type": "default",
                "value": {
                    "action": "summarize_video",
                    "url": action_url,
                },
            }
        )

    if _supports_comment_analysis_action(meta):
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "分析评论"},
                "type": "default",
                "value": {
                    "action": "analyze_comments",
                    "url": action_url,
                },
            }
        )

    return actions


def _action_url(meta: LinkMetadata) -> str:
    return meta.canonical_url or meta.source_url


def _supports_summary_action(meta: LinkMetadata) -> bool:
    platform = meta.platform.strip().lower()
    if platform in {"bilibili", "youtube"}:
        return True

    url = meta.source_url.lower()
    return any(domain in url for domain in ("bilibili.com", "b23.tv", "youtube.com", "youtu.be"))


def _supports_comment_analysis_action(meta: LinkMetadata) -> bool:
    platform = meta.platform.strip().lower()
    if platform in {"bilibili", "instagram", "tiktok", "youtube", "x"}:
        return True

    url = meta.source_url.lower()
    return any(
        domain in url
        for domain in (
            "bilibili.com",
            "b23.tv",
            "instagram.com",
            "tiktok.com",
            "youtube.com",
            "youtu.be",
            "x.com",
            "twitter.com",
        )
    )


def _description_should_be_primary(meta: LinkMetadata, description_block: str) -> bool:
    return bool(
        description_block and meta.media_type == MediaType.ARTICLE and meta.platform != "web"
    )


def _format_description_block(meta: LinkMetadata) -> str:
    if meta.media_type == MediaType.VIDEO:
        return ""

    description = _clean_description_text(meta.description)
    translated = _clean_description_text(meta.translated_description)
    if translated and description:
        return (
            f"{escape(_truncate(translated, 120))} "
            f"<font color='grey'>原文: {escape(_truncate(description, 160))}</font>"
        )
    if description and meta.media_type == MediaType.ARTICLE:
        return f"<font color='grey'>{escape(_truncate(description, 160))}</font>"
    return ""


def _clean_description_text(text: str) -> str:
    return " ".join(_strip_emoji_symbols(text).split())


def _normalize_summary_markdown(markdown: str) -> str:
    content = _strip_emoji_symbols(markdown).strip()
    if not content:
        return "No summary content."

    indent_unit = _detect_markdown_indent_unit(content)
    lines: list[str] = []
    for line in content.splitlines():
        lines.append(_normalize_summary_markdown_line(line, indent_unit=indent_unit))
    return "\n".join(lines).strip()


def _normalize_summary_markdown_line(line: str, *, indent_unit: int) -> str:
    stripped = line.lstrip(" ")
    if not stripped:
        return ""

    indent = len(line) - len(stripped)
    bullet_indent = _normalize_bullet_indent(indent, indent_unit=indent_unit)
    heading_match = re.match(r"#{1,6}\s+(.+?)\s*#*\s*$", stripped)
    if heading_match:
        return f"{bullet_indent}- {heading_match.group(1).strip()}"

    bullet_match = re.match(r"[*+-]\s+(.+)$", stripped)
    if bullet_match:
        return f"{bullet_indent}- {bullet_match.group(1)}"

    return line


def _detect_markdown_indent_unit(markdown: str) -> int:
    indents: list[int] = []
    for line in markdown.splitlines():
        stripped = line.lstrip(" ")
        if not stripped:
            continue
        if not re.match(r"(?:#{1,6}\s+|[*+-]\s+)", stripped):
            continue
        indent = len(line) - len(stripped)
        if indent > 0:
            indents.append(indent)
    return min(indents) if indents else 2


def _normalize_bullet_indent(indent: int, *, indent_unit: int) -> str:
    if indent <= 0:
        return ""
    level = max(1, round(indent / max(1, indent_unit)))
    return " " * (level * 4)


def _strip_emoji_symbols(text: str) -> str:
    return "".join(ch for ch in text if not _is_emoji_or_modifier(ch))


def _split_markdown(markdown: str) -> tuple[str, list[tuple[str, str]]]:
    """Split normalized markdown into (preamble, sections).

    Preamble is any content before the first top-level bullet; sections are
    (title, body) pairs split on top-level '- ' lines.
    """
    preamble_lines: list[str] = []
    sections: list[tuple[str, str]] = []
    current_title: str | None = None
    current_body: list[str] = []

    for line in markdown.splitlines():
        if line and not line.startswith(" ") and line.startswith("- "):
            if current_title is not None:
                sections.append((current_title, "\n".join(current_body).strip()))
            current_title = re.sub(r"\*\*(.+?)\*\*", r"\1", line[2:]).strip()
            current_body = []
        elif current_title is not None:
            stripped = line[4:] if line.startswith("    ") else line
            current_body.append(stripped)
        else:
            preamble_lines.append(line)

    if current_title is not None:
        sections.append((current_title, "\n".join(current_body).strip()))

    return "\n".join(preamble_lines).strip(), sections


_PLACEHOLDER_BODIES = {"无", "无内容", "n/a", "none", ""}


def _section_has_content(body: str) -> bool:
    return body.strip().lower() not in _PLACEHOLDER_BODIES


def _build_section_panel(title: str, body: str) -> dict[str, object]:
    safe_title = _strip_emoji_symbols(title).strip() or "内容"
    return {
        "tag": "collapsible_panel",
        "expanded": False,
        "header": {
            "title": {"tag": "markdown", "content": f"**{safe_title}**"},
            "vertical_align": "center",
            "padding": "4px 0px 4px 8px",
        },
        "elements": [{"tag": "markdown", "content": body or "(无内容)"}],
    }


def _is_emoji_or_modifier(ch: str) -> bool:
    codepoint = ord(ch)
    return (
        unicodedata.category(ch) == "So"
        or 0x1F3FB <= codepoint <= 0x1F3FF
        or codepoint in {0x200D, 0xFE0E, 0xFE0F, 0x20E3}
    )


def _build_compact_media_row(
    img_key: str,
    text_element: dict[str, object],
) -> dict[str, object]:
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": "default",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "elements": [
                    {
                        "tag": "img",
                        "img_key": img_key,
                        "alt": {"tag": "plain_text", "content": ""},
                        "mode": "crop_center",
                        "preview": True,
                        "compact_width": True,
                    }
                ],
            },
            {
                "tag": "column",
                "width": "weighted",
                "weight": 3,
                "vertical_align": "top",
                "elements": [text_element],
            },
        ],
    }
