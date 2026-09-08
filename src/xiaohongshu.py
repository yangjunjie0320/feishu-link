"""Xiaohongshu note identity, separate from signed access URLs."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit, urlunsplit


def note_id(url: str) -> str:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not (
        host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com")
    ):
        return ""
    match = re.fullmatch(r"/(?:explore|discovery/item)/([a-fA-F0-9]{24})/?", parsed.path)
    return match[1].lower() if match else ""


def access_url(url: str) -> str:
    """Preserve path and query: signatures are needed by the origin and BibiGPT."""
    parsed = urlsplit(url.strip())
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def note_key(url: str) -> str:
    identity = note_id(url)
    return f"xiaohongshu:{identity}" if identity else ""


def prefer_access_url(candidate: str, fallback: str) -> str:
    """Use a fresh signed alias, without discarding an already available signature."""
    if not note_id(candidate) or note_id(candidate) != note_id(fallback):
        return fallback
    if parse_qs(urlsplit(candidate).query).get("xsec_token"):
        return access_url(candidate)
    if parse_qs(urlsplit(fallback).query).get("xsec_token"):
        return fallback
    return access_url(candidate)
