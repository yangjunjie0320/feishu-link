from __future__ import annotations

import json

from .parsers.base import LinkMetadata


def _fmt_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _truncate(text: str, max_chars: int) -> str:
    return text[:max_chars].rstrip() + "..." if len(text) > max_chars else text


def build_card(meta: LinkMetadata) -> str:
    elements: list[dict] = []

    if meta.cover_url:
        elements.append({
            "tag": "img",
            "img_key": meta.cover_url,
            "alt": {"tag": "plain_text", "content": meta.title or "cover"},
            "mode": "fit_horizontal",
        })

    subtitle_parts: list[str] = []
    if meta.site_name:
        subtitle_parts.append(meta.site_name)
    if meta.channel:
        subtitle_parts.append(meta.channel)
    if meta.duration_seconds is not None:
        subtitle_parts.append(_fmt_duration(meta.duration_seconds))
    subtitle = " · ".join(subtitle_parts)

    body_lines: list[str] = []
    if subtitle:
        body_lines.append(f"**{_truncate(meta.title, 80)}**")
        body_lines.append(subtitle)
    else:
        body_lines.append(f"**{_truncate(meta.title, 80)}**")

    if meta.description:
        body_lines.append(_truncate(meta.description, 120))

    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": "\n".join(body_lines)},
    })

    elements.append({
        "tag": "action",
        "actions": [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": "打开链接"},
            "url": meta.source_url,
            "type": "default",
        }],
    })

    card: dict = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": _truncate(meta.title, 50) or meta.site_name or "Link",
            },
            "template": "blue",
        },
        "elements": elements,
    }

    return json.dumps(card, ensure_ascii=False)
