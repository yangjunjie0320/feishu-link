from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .cookie_utils import temporary_cookie_file
from .parsers.base import LinkMetadata, MediaType

logger = logging.getLogger(__name__)


class VideoSkipReason(Exception):
    pass


class VideoDownloadError(Exception):
    pass


@dataclass
class DownloadedVideo:
    path: Path
    file_name: str
    duration_ms: int
    temp_dir: Path

    def cleanup(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)


def explain_video_skip(meta: LinkMetadata, settings: Settings) -> str | None:
    if not settings.video_append_enabled:
        return "video append disabled"
    if meta.platform not in settings.allowed_video_platforms:
        return f"platform not allowed: {meta.platform}"
    if meta.media_type != MediaType.VIDEO:
        return f"not a video: media_type={meta.media_type}"
    if meta.duration_seconds is None:
        return "video duration unknown"
    if meta.duration_seconds > settings.max_video_duration_seconds:
        return (
            "video duration exceeds limit: "
            f"duration={meta.duration_seconds} limit={settings.max_video_duration_seconds}"
        )
    if meta.requires_auth and not settings.cookie_file_for_platform(meta.platform):
        return f"platform requires auth: platform={meta.platform}"
    if not meta.download_candidates:
        return "no download candidate"
    return None


async def download_video(meta: LinkMetadata, settings: Settings) -> DownloadedVideo:
    skip_reason = explain_video_skip(meta, settings)
    if skip_reason:
        raise VideoSkipReason(skip_reason)

    source_url = meta.canonical_url or meta.source_url
    temp_root = Path(settings.video_temp_dir)
    temp_root.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="video-", dir=temp_root))

    def _download() -> Path:
        try:
            import yt_dlp
        except ModuleNotFoundError as e:
            raise VideoDownloadError("yt-dlp is not installed") from e

        cookie_file = settings.cookie_file_for_platform(meta.platform)
        with temporary_cookie_file(cookie_file) as temp_cookie_file:
            options: dict[str, Any] = {
                "format": "best[ext=mp4]/best",
                "merge_output_format": "mp4",
                "noplaylist": True,
                "outtmpl": str(temp_dir / "%(title).80s-%(id)s.%(ext)s"),
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "logger": _YtDlpLogger(),
            }
            if temp_cookie_file:
                options["cookiefile"] = temp_cookie_file

            before = set(temp_dir.iterdir())
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    ydl.download([source_url])
            except Exception as e:
                raise VideoDownloadError(f"yt-dlp download failed: {e}") from e

        after = [p for p in temp_dir.iterdir() if p.is_file() and p not in before]
        if not after:
            raise VideoDownloadError("download finished without output file")
        return max(after, key=lambda p: p.stat().st_size)

    loop = asyncio.get_running_loop()
    try:
        path = await loop.run_in_executor(None, _download)
        path = await loop.run_in_executor(None, lambda: _make_feishu_mp4(path, temp_dir))
        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > settings.max_video_file_mb:
            raise VideoDownloadError(
                f"downloaded video too large: size_mb={size_mb:.2f} "
                f"limit_mb={settings.max_video_file_mb}"
            )
        return DownloadedVideo(
            path=path,
            file_name=_file_name(path),
            duration_ms=_probe_duration_ms(path),
            temp_dir=temp_dir,
        )
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def _file_name(path: Path) -> str:
    name = _strip_symbol_characters(path.name).strip()
    if not name or name == path.suffix:
        name = f"video{path.suffix or '.mp4'}"
    if len(name) <= 120:
        return name
    return f"{name[:100].rstrip()}{path.suffix}"


def _strip_symbol_characters(value: str) -> str:
    return "".join(ch for ch in value if unicodedata.category(ch) != "So")


def _make_feishu_mp4(path: Path, temp_dir: Path) -> Path:
    output = temp_dir / f"{path.stem}.feishu.mp4"
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(output),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=180)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        stderr = getattr(e, "stderr", "") or str(e)
        raise VideoDownloadError(f"ffmpeg transcode failed: {stderr[:500]}") from e
    if not output.exists() or output.stat().st_size == 0:
        raise VideoDownloadError("ffmpeg transcode produced empty output")
    return output


def _probe_duration_ms(path: Path) -> int:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)
        seconds = float(result.stdout.strip())
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as e:
        raise VideoDownloadError(f"ffprobe duration failed: {e}") from e
    return max(1, round(seconds * 1000))


class _YtDlpLogger:
    def debug(self, msg: str) -> None:
        logger.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        logger.info("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        logger.warning("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        logger.error("yt-dlp: %s", msg)
