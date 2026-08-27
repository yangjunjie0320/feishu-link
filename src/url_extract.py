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

# Feishu @mention placeholder pattern in text content: @_user_1 etc.
_MENTION_PLACEHOLDER_RE = re.compile(r"@_user_\d+")
_MENTION_TAIL_RE = re.compile(r"@_user_\d+.*$")


def extract_urls(message_type: str, content_raw: str, settings: Settings) -> list[str]:
    text = _decode_content(message_type, content_raw)
    if not text:
        return []

    seen: set[str] = set()
    result: list[str] = []
    for m in _URL_RE.finditer(text):
        url = _clean_url_match(m.group(0))
        if url in seen:
            continue
        seen.add(url)
        if _SKIP_DOMAINS.match(url):
            continue
        if not settings.is_allowed(url):
            continue
        if settings.is_blacklisted(url):
            continue
        result.append(url)
    return result


def _clean_url_match(raw: str) -> str:
    return _MENTION_TAIL_RE.sub("", raw).rstrip(".,;:!?)\"'")


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


def decode_plain_text(message_type: str, raw: str) -> str:
    """Decode a text/post message to plain text with @mention placeholders
    stripped; used for reply remarks."""
    text = _decode_content(message_type, raw)
    return _MENTION_PLACEHOLDER_RE.sub("", text).strip()


def extract_prompt(message_type: str, raw: str, video_url: str) -> str | None:
    """Extract custom prompt from message text.

    Removes the video URL and @mention placeholders, returns remaining text
    as the custom prompt, or None if nothing meaningful remains.
    """
    text = _decode_content(message_type, raw)
    remaining = text.replace(video_url, "")
    remaining = _MENTION_PLACEHOLDER_RE.sub("", remaining)
    remaining = remaining.strip()

    # If only whitespace/punctuation remains, no custom prompt.
    if not remaining or len(remaining) < 2:
        return None

    return remaining


def is_manual_download_command(message_type: str, raw: str) -> bool:
    text = _decode_content(message_type, raw)
    text = _MENTION_PLACEHOLDER_RE.sub("", text).strip()
    if not text.startswith("下载"):
        return False

    tail = text.removeprefix("下载").lstrip(" \t\n\r:").lstrip("\N{FULLWIDTH COLON}")
    return bool(_URL_RE.search(tail))
