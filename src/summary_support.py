from __future__ import annotations

import re
from urllib.parse import parse_qs, urlencode, urlparse

from .card_metadata import is_placeholder
from .parsers.base import LinkMetadata, MediaType
from .platforms import normalize_url

_VIDEO_ID = re.compile(r"[0-9]+")
_YOUTUBE_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_BILIBILI_ID = re.compile(r"BV[A-Za-z0-9]{10}|av[0-9]+")
_SUPPORTED_DOMAINS = {
    "bilibili": ("bilibili.com", "b23.tv"),
    "youtube": ("youtube.com", "youtu.be"),
    "tiktok": ("tiktok.com",),
    "douyin": ("douyin.com", "iesdouyin.com"),
}


def _summary_platform(url: str) -> str:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"}:
        return ""
    for platform, domains in _SUPPORTED_DOMAINS.items():
        if any(host == domain or host.endswith(f".{domain}") for domain in domains):
            return platform
    return ""


def canonical_summary_url(url: str) -> str:
    """Normalize known video URLs without resolving links or changing photo content."""
    platform = _summary_platform(url.strip())
    if not platform:
        return url.strip()
    parsed = urlparse(url.strip())
    parts = [part for part in parsed.path.split("/") if part]
    query = parse_qs(parsed.query)
    if platform == "youtube":
        video_id = ""
        if parsed.hostname in {"youtu.be", "www.youtu.be"} and len(parts) == 1:
            video_id = parts[0]
        elif len(parts) == 2 and parts[0] in {"shorts", "embed", "live"}:
            video_id = parts[1]
        elif parts == ["watch"]:
            video_id = query.get("v", [""])[0]
        if _YOUTUBE_ID.fullmatch(video_id):
            return f"https://www.youtube.com/watch?v={video_id}"
    elif platform == "bilibili":
        if len(parts) == 2 and parts[0] == "video" and _BILIBILI_ID.fullmatch(parts[1]):
            canonical = f"https://www.bilibili.com/video/{parts[1]}"
            page = query.get("p", [""])[0]
            if _VIDEO_ID.fullmatch(page) and page.lstrip("0") not in {"", "1"}:
                canonical += "?" + urlencode({"p": page.lstrip("0")})
            return canonical
    elif platform == "tiktok":
        if (
            len(parts) == 3 and parts[0].startswith("@")
            and parts[1] in {"video", "photo"} and _VIDEO_ID.fullmatch(parts[2])
        ):
            return f"https://www.tiktok.com/{'/'.join(parts)}"
        if len(parts) == 2 and parts[0] in {"video", "photo"} and _VIDEO_ID.fullmatch(parts[1]):
            return f"https://www.tiktok.com/{'/'.join(parts)}"
    elif platform == "douyin":
        content_parts = parts[1:] if parts[:1] == ["share"] else parts
        if (
            len(content_parts) == 2 and content_parts[0] in {"video", "note"}
            and _VIDEO_ID.fullmatch(content_parts[1])
        ):
            return f"https://www.douyin.com/{'/'.join(content_parts)}"
        # A modal link may live on a profile or the homepage, but an explicit
        # photo/note path must never be rewritten as a video by its query string.
        modal_id = query.get("modal_id", [""])[0]
        if not {"photo", "note", "slides"}.intersection(parts) and _VIDEO_ID.fullmatch(modal_id):
            return f"https://www.douyin.com/video/{modal_id}"
    return normalize_url(url)


def summary_url_for_metadata(meta: LinkMetadata) -> str:
    """Keep the shared Bilibili part when page metadata omits its selection."""
    canonical = canonical_summary_url(meta.canonical_url or meta.source_url)
    if _summary_platform(canonical) != "bilibili" or not is_summary_video_url(canonical):
        return canonical
    if _summary_platform(meta.source_url) != "bilibili":
        return canonical
    source = urlparse(meta.source_url)
    page = parse_qs(source.query).get("p", [""])[0]
    if not _VIDEO_ID.fullmatch(page) or page.lstrip("0") in {"", "1"}:
        return canonical
    canonical_path = urlparse(canonical).path
    source_path = urlparse(canonical_summary_url(meta.source_url)).path
    same_video = source_path == canonical_path and is_summary_video_url(meta.source_url)
    resolved_short_link = (
        source.hostname in {"b23.tv", "www.b23.tv"}
        and len([part for part in source.path.split("/") if part]) == 1
    )
    if same_video or resolved_short_link:
        return canonical.split("?", 1)[0] + "?" + urlencode({"p": page.lstrip("0")})
    return canonical


def is_summary_video_url(url: str) -> bool:
    """Whether the URL identifies a supported video, rather than a share or photo page."""
    canonical = canonical_summary_url(url)
    platform = _summary_platform(canonical)
    if not platform:
        return False
    parsed = urlparse(canonical)
    parts = [part for part in parsed.path.split("/") if part]
    if platform == "youtube":
        return parts == ["watch"] and bool(
            _YOUTUBE_ID.fullmatch(parse_qs(parsed.query).get("v", [""])[0])
        )
    if platform == "bilibili":
        return len(parts) == 2 and parts[0] == "video" and bool(
            _BILIBILI_ID.fullmatch(parts[1])
        )
    if platform == "tiktok" and len(parts) == 3 and parts[0].startswith("@"):
        parts = parts[1:]
    return len(parts) == 2 and parts[0] == "video" and bool(_VIDEO_ID.fullmatch(parts[1]))


def supports_video_summary(meta: LinkMetadata) -> bool:
    """Expose summarization only for supported videos with real parsed content."""
    if meta.media_type != MediaType.VIDEO:
        return False
    url = meta.canonical_url or meta.source_url
    platform = _summary_platform(url)
    if not platform or meta.platform.strip().lower() not in {"", "web", platform}:
        return False
    if platform in {"tiktok", "douyin"} and not is_summary_video_url(url):
        return False
    real_text = any(
        value.strip() != "内容暂未获取" and not is_placeholder(value, platform, url)
        for value in (meta.title, meta.description)
    )
    return real_text or bool(meta.content_verified and (meta.cover_url or meta.cover_candidates))
