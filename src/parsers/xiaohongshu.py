"""Parse the exact native note object, never a recommended note or OG type."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..xiaohongshu import access_url, note_id, prefer_access_url
from .base import LinkMetadata, MediaType, ParserError


def parse_note(url: str, html: str, final_url: str) -> LinkMetadata:
    expected, landed = note_id(url), note_id(final_url)
    if not landed or (expected and expected != landed):
        raise ParserError(url, "target_mismatch: note identity differs from final URL")
    soup = BeautifulSoup(html[:8_000_000], "lxml")
    for tag in soup.select('meta[property="og:url"], link[rel="canonical"]'):
        declared = str(tag.get("content") or tag.get("href") or "")
        if declared and note_id(urljoin(final_url, declared)) != landed:
            raise ParserError(url, "target_mismatch: canonical identifies another note")
    for script in soup.find_all("script"):
        raw = script.string or script.get_text()
        match = re.search(r"(?:window\.)?__INITIAL_STATE__\s*=\s*", raw)
        if not match:
            continue
        # JS undefined occurs in this otherwise JSON object. Replace tokens only,
        # preserving quoted strings; never evaluate page JavaScript.
        value = re.sub(
            r'"(?:[^"\\]|\\.)*"|\bundefined\b',
            lambda token: "null" if token[0] == "undefined" else token[0],
            raw[match.end():],
        )
        try:
            state, _ = json.JSONDecoder().raw_decode(value)
        except ValueError:
            continue
        if not isinstance(state, dict):
            continue
        notes = state.get("note", {})
        notes = notes.get("noteDetailMap", {}) if isinstance(notes, dict) else {}
        entry = notes.get(landed, {}) if isinstance(notes, dict) else {}
        note = entry.get("note", {}) if isinstance(entry, dict) else {}
        if isinstance(note, dict) and note.get("noteId") == landed:
            return _metadata(url, final_url, note)
    text = soup.get_text(" ", strip=True)
    for markers, reason in (
        (("笔记已删除", "笔记不存在", "无法查看当前笔记"), "deleted"),
        (("安全验证", "请完成验证", "访问频次异常"), "challenge"),
        (("登录后查看", "扫码登录", "登录探索更多内容"), "auth"),
    ):
        if any(marker in text for marker in markers):
            raise ParserError(url, f"{reason}: target note unavailable")
    raise ParserError(url, "no_content: no native target note")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _metadata(source_url: str, final_url: str, note: dict[str, Any]) -> LinkMetadata:
    kind = note.get("type")
    media_type = {"normal": MediaType.ARTICLE, "video": MediaType.VIDEO}.get(
        kind, MediaType.UNKNOWN,
    )
    images = note.get("imageList")
    first = images[0] if isinstance(images, list) and images else {}
    covers: list[str] = []
    if isinstance(first, dict):
        infos = first.get("infoList")
        candidates = [first.get("urlDefault"), first.get("urlPre"), first.get("url")]
        if isinstance(infos, list):
            candidates.extend(item.get("url") for item in infos if isinstance(item, dict))
        for candidate in candidates:
            value = _text(candidate)
            if value.startswith(("http://", "https://")) and value not in covers:
                covers.append(value)
    user = note.get("user")
    return LinkMetadata(
        source_url=source_url, canonical_url=prefer_access_url(source_url, access_url(final_url)),
        platform="xiaohongshu", site_name="小红书", title=_text(note.get("title")),
        description=_text(note.get("desc")),
        channel=_text(user.get("nickname")) if isinstance(user, dict) else None,
        media_type=media_type, content_verified=True,
        has_visual=True if images else None,
        cover_url=next(iter(covers), ""), cover_candidates=covers[:3],
    )
