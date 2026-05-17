from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..config import Settings
from ..cookie_utils import temporary_cookie_file
from ..platforms import detect_platform
from .base import DownloadCandidate, LinkMetadata, MediaType, ParserError

logger = logging.getLogger(__name__)


class YtDlpMetadataParser:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def parse(self, url: str) -> LinkMetadata:
        platform = detect_platform(url)

        def _extract() -> dict[str, Any]:
            try:
                import yt_dlp
            except ModuleNotFoundError as e:
                raise ParserError(url, "yt-dlp is not installed") from e

            cookie_file = self._settings.cookie_file_for_platform(platform)
            with temporary_cookie_file(cookie_file) as temp_cookie_file:
                options: dict[str, Any] = {
                    "quiet": True,
                    "no_warnings": True,
                    "skip_download": True,
                    "noplaylist": True,
                    "noprogress": True,
                    "logger": _YtDlpLogger(),
                }
                if temp_cookie_file:
                    options["cookiefile"] = temp_cookie_file

                try:
                    with yt_dlp.YoutubeDL(options) as ydl:
                        info = ydl.extract_info(url, download=False)
                except Exception as e:
                    raise ParserError(url, f"yt-dlp metadata failed: {e}") from e

            if not isinstance(info, dict):
                raise ParserError(url, "yt-dlp returned invalid metadata")
            return info

        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, _extract)
        return _metadata_from_info(url, platform, info)


def _metadata_from_info(url: str, platform: str, info: dict[str, Any]) -> LinkMetadata:
    duration = _as_int(info.get("duration"))
    candidates = _download_candidates(info)
    warnings: list[str] = []
    if not candidates:
        warnings.append("no download candidate found")

    return LinkMetadata(
        source_url=url,
        title=str(info.get("title") or info.get("fulltitle") or ""),
        description=str(info.get("description") or "")[:300],
        cover_url=str(info.get("thumbnail") or ""),
        site_name=_site_name(platform, info),
        platform=platform,
        canonical_url=str(info.get("webpage_url") or info.get("original_url") or url),
        media_type=MediaType.VIDEO if _looks_like_video(info, candidates) else MediaType.UNKNOWN,
        duration_seconds=duration,
        channel=str(info.get("uploader") or info.get("channel") or "") or None,
        view_count=_as_int(info.get("view_count")),
        like_count=_as_int(info.get("like_count")),
        comment_count=_as_int(info.get("comment_count")),
        repost_count=_as_int(info.get("repost_count") or info.get("share_count")),
        download_candidates=candidates,
        requires_auth=_requires_auth(info),
        parse_warnings=warnings,
    )


def _download_candidates(info: dict[str, Any]) -> list[DownloadCandidate]:
    candidates: list[DownloadCandidate] = []
    for item in info.get("formats") or []:
        if not isinstance(item, dict):
            continue
        candidate_url = item.get("url")
        if not candidate_url:
            continue
        candidates.append(
            DownloadCandidate(
                url=str(candidate_url),
                format_id=str(item.get("format_id") or ""),
                ext=str(item.get("ext") or ""),
                filesize=_as_int(item.get("filesize") or item.get("filesize_approx")),
            )
        )

    if not candidates and info.get("url"):
        candidates.append(
            DownloadCandidate(
                url=str(info["url"]),
                format_id=str(info.get("format_id") or ""),
                ext=str(info.get("ext") or ""),
                filesize=_as_int(info.get("filesize") or info.get("filesize_approx")),
            )
        )
    return candidates


def _looks_like_video(info: dict[str, Any], candidates: list[DownloadCandidate]) -> bool:
    if info.get("duration") is not None:
        return True
    if info.get("vcodec") and info.get("vcodec") != "none":
        return True
    return bool(candidates)


def _requires_auth(info: dict[str, Any]) -> bool:
    availability = str(info.get("availability") or "").lower()
    return availability in {"subscriber_only", "premium_only", "needs_auth", "private"}


def _site_name(platform: str, info: dict[str, Any]) -> str:
    if platform == "bilibili":
        return "Bilibili"
    if platform == "instagram":
        return "Instagram"
    if platform == "tiktok":
        return "TikTok"
    if platform == "youtube":
        return "YouTube"
    if platform == "x":
        return "X"
    return str(info.get("extractor_key") or platform or "Link")


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


class _YtDlpLogger:
    def debug(self, msg: str) -> None:
        logger.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        logger.info("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        logger.warning("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        logger.error("yt-dlp: %s", msg)
