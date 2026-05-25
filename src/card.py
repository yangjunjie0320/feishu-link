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
) -> str:
    safe_title = _strip_emoji_symbols(title).strip() or "Summary"
    content = _normalize_summary_markdown(markdown)
    elements: list[dict[str, object]] = [{
        "tag": "markdown",
        "content": content,
    }]

    if source_url:
        elements.append({
            "tag": "markdown",
            "content": f"[打开视频]({source_url})",
        })

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

    elements.append({
        "tag": "action",
        "actions": _build_actions(meta),
    })

    card: dict[str, object] = {
        "config": {"wide_screen_mode": True},
        "elements": elements,
    }

    return json.dumps(card, ensure_ascii=False)


def _build_actions(meta: LinkMetadata) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "打开链接"},
            "url": meta.source_url,
            "type": "primary",
        }
    ]

    if meta.media_type == MediaType.VIDEO:
        if _supports_summary_action(meta):
            actions.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "总结视频"},
                "type": "default",
                "value": {
                    "action": "summarize_video",
                    "url": meta.source_url,
                },
            })

        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "下载视频"},
            "type": "default",
            "value": {
                "action": "download_video",
                "url": meta.source_url,
            },
        })

    return actions


def _supports_summary_action(meta: LinkMetadata) -> bool:
    platform = meta.platform.strip().lower()
    if platform in {"bilibili", "youtube"}:
        return True

    url = meta.source_url.lower()
    return any(
        domain in url
        for domain in ("bilibili.com", "b23.tv", "youtube.com", "youtu.be")
    )


def _description_should_be_primary(meta: LinkMetadata, description_block: str) -> bool:
    return bool(
        description_block
        and meta.media_type == MediaType.ARTICLE
        and meta.platform != "web"
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

    lines: list[str] = []
    for line in content.splitlines():
        lines.append(_normalize_summary_markdown_line(line))
    return "\n".join(lines).strip()


def _normalize_summary_markdown_line(line: str) -> str:
    stripped = line.lstrip(" ")
    if not stripped:
        return ""

    indent = len(line) - len(stripped)
    heading_match = re.match(r"#{1,6}\s+(.+?)\s*#*\s*$", stripped)
    if heading_match:
        return f"{_normalize_bullet_indent(indent)}- {heading_match.group(1).strip()}"

    bullet_match = re.match(r"[*+-]\s+(.+)$", stripped)
    if bullet_match:
        return f"{_normalize_bullet_indent(indent)}- {bullet_match.group(1)}"

    return line


def _normalize_bullet_indent(indent: int) -> str:
    if indent <= 0:
        return ""
    level = indent // 4 if indent % 4 == 0 else max(1, indent // 2)
    return " " * (level * 4)


def _strip_emoji_symbols(text: str) -> str:
    return "".join(ch for ch in text if not _is_emoji_or_modifier(ch))


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
                "elements": [{
                    "tag": "img",
                    "img_key": img_key,
                    "alt": {"tag": "plain_text", "content": ""},
                    "mode": "crop_center",
                    "preview": True,
                    "compact_width": True,
                }],
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
