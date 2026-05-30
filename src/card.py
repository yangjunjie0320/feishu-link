from __future__ import annotations

import json
import re
import unicodedata
from html import escape

from .parsers.base import LinkMetadata, MediaType

_SOURCE_COLORS = {
    "youtube": "red",
    "github": "grey",
    "twitter": "blue",
    "x": "blue",
    "bilibili": "blue",
    "instagram": "purple",
    "tiktok": "grey",
    "zhihu": "blue",
}


def _fmt_duration(seconds: int) -> str:
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
                _build_section_panel(sec_title, sec_body) for sec_title, sec_body in sections
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

    if source_url:
        elements.append(
            {
                "tag": "markdown",
                "content": f"[打开视频]({source_url})",
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


def build_card(meta: LinkMetadata, img_key: str | None = None) -> str:
    elements: list[dict[str, object]] = []

    meta_parts: list[str] = []
    source_label = meta.site_name or meta.platform
    if source_label:
        meta_parts.append(source_label)
    if meta.channel:
        meta_parts.append(meta.channel)
    if meta.duration_seconds is not None:
        meta_parts.append(_fmt_duration(meta.duration_seconds))

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

    elements.append(
        {
            "tag": "action",
            "actions": _build_actions(meta),
        }
    )

    card: dict[str, object] = {
        "config": {"wide_screen_mode": True},
        "elements": elements,
    }

    return json.dumps(card, ensure_ascii=False)


def _build_actions(meta: LinkMetadata) -> list[dict[str, object]]:
    action_url = _action_url(meta)
    actions: list[dict[str, object]] = [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "打开链接"},
            "url": action_url,
            "type": "primary",
        }
    ]

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

    if meta.media_type == MediaType.VIDEO:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "下载视频"},
                "type": "default",
                "value": {
                    "action": "download_video",
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


def _split_into_sections(markdown: str) -> list[tuple[str, str]]:
    """Split normalized markdown into (title, body) pairs on top-level bullets."""
    _, sections = _split_markdown(markdown)
    return sections


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
        or codepoint in {0x200D, 0xFE0E, 0xFE0F}
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
