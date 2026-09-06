from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from datetime import datetime
from typing import Any

from ..config import Settings
from ..cookie_refresh import ensure_fresh_cookies
from ..cookie_utils import temporary_cookie_file
from ..platforms import detect_platform
from .base import DownloadCandidate, LinkMetadata, MediaType, ParserError

logger = logging.getLogger(__name__)


class YtDlpMetadataParser:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._slots = asyncio.Semaphore(settings.media_metadata_concurrency)

    async def parse(self, url: str) -> LinkMetadata:
        platform = detect_platform(url)
        try:
            async with asyncio.timeout(self._settings.media_metadata_timeout), self._slots:
                await ensure_fresh_cookies(platform, self._settings)
                cookie_file = self._settings.cookie_file_for_platform(platform)
                with temporary_cookie_file(cookie_file) as temporary:
                    data = await _run_metadata_worker(url, platform, temporary or "")
        except TimeoutError as exc:
            raise ParserError(url, "media metadata time budget exhausted") from exc
        try:
            data["media_type"] = MediaType(data.get("media_type", "unknown"))
            data["download_candidates"] = [
                DownloadCandidate(**item) for item in data.get("download_candidates", [])
            ]
            if "fetched_at_utc" in data:
                data["fetched_at_utc"] = datetime.fromisoformat(data["fetched_at_utc"])
            return LinkMetadata(**data)
        except (TypeError, ValueError, KeyError) as exc:
            raise ParserError(url, "media worker returned invalid metadata") from exc


async def _stop_worker(process: asyncio.subprocess.Process) -> None:
    # A finished Python worker can leave a child holding its output pipes.
    # Its private process group still needs cleanup in that case.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=1)
    except TimeoutError:
        pass
    finally:
        # Deno may outlive the Python worker; reap the entire private group.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            logger.warning("media worker group cleanup denied: pid=%s", process.pid)
    if process.returncode is None:
        await asyncio.wait_for(process.wait(), timeout=1)


async def _run_metadata_worker(url: str, platform: str, cookie_file: str) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "src.parsers.ytdlp_worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        payload = json.dumps({"url": url, "platform": platform, "cookie_file": cookie_file})
        output, errors = await process.communicate(payload.encode())
    except BaseException:
        await asyncio.shield(_stop_worker(process))
        raise
    if process.returncode:
        raise ParserError(url, "yt-dlp metadata failed: " + errors.decode(errors="replace")[-600:])
    try:
        data = json.loads(output)
    except ValueError as exc:
        raise ParserError(url, "media worker returned non-JSON output") from exc
    if not isinstance(data, dict):
        raise ParserError(url, "media worker returned invalid output")
    return data


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
        cover_url=_cover_url_from_info(info),
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


def _cover_url_from_info(info: dict[str, Any]) -> str:
    thumbnail = info.get("thumbnail")
    if thumbnail:
        return str(thumbnail)

    return _find_image_url(info)


def _find_image_url(value: object) -> str:
    if isinstance(value, dict):
        for key in ("thumbnail", "url"):
            candidate = value.get(key)
            if _looks_like_image_url(candidate):
                return str(candidate)
        for key in ("thumbnails", "entries", "formats", "requested_downloads"):
            nested = _find_image_url(value.get(key))
            if nested:
                return nested
        for nested_value in value.values():
            nested = _find_image_url(nested_value)
            if nested:
                return nested
    if isinstance(value, list):
        for item in value:
            nested = _find_image_url(item)
            if nested:
                return nested
    return ""


def _looks_like_image_url(value: object) -> bool:
    if not value:
        return False
    text = str(value).lower()
    if not text.startswith(("http://", "https://")):
        return False
    path = text.split("?", 1)[0]
    return path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))


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
