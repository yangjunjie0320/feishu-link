import json
import re
from typing import Any

from .config import Settings

_URL_RE = re.compile(
    r"https?://"
    r"(?:[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%])"
    r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]*",
    re.IGNORECASE,
)

# Feishu rich-text "at" and mention patterns to skip
_SKIP_DOMAINS = re.compile(r"https?://(?:open\.feishu\.cn|open\.larksuite\.com)")


def extract_urls(message_type: str, content_raw: str, settings: Settings) -> list[str]:
    text = _decode_content(message_type, content_raw)
    if not text:
        return []

    seen: set[str] = set()
    result: list[str] = []
    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip(".,;:!?)\"'")
        if url in seen:
            continue
        seen.add(url)
        if _SKIP_DOMAINS.match(url):
            continue
        if settings.is_blacklisted(url):
            continue
        result.append(url)
    return result


def _decode_content(message_type: str, raw: str) -> str:
    if message_type == "text":
        try:
            obj: Any = json.loads(raw)
            return str(obj.get("text", "")) if isinstance(obj, dict) else raw
        except (json.JSONDecodeError, AttributeError):
            return raw

    if message_type == "post":
        try:
            obj = json.loads(raw)
            return _flatten_post(obj)
        except (json.JSONDecodeError, TypeError):
            return raw

    return raw


def _flatten_post(obj: Any) -> str:
    parts: list[str] = []
    content = obj.get("content", []) if isinstance(obj, dict) else []
    for line in content:
        if not isinstance(line, list):
            continue
        for span in line:
            if isinstance(span, dict) and span.get("tag") == "a":
                parts.append(span.get("href", ""))
            elif isinstance(span, dict):
                parts.append(str(span.get("text", "")))
    return " ".join(parts)
