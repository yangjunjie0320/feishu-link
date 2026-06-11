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
from .ytdlp_options import apply_ytdlp_runtime

logger = logging.getLogger(__name__)

_YTDLP_VIDEO_FORMAT = (
    "worst[ext=mp4]/"
    "worstvideo[ext=mp4]+worstaudio[ext=m4a]/"
    "worstvideo+worstaudio/"
    "worst"
)
_TIKTOK_YTDLP_VIDEO_FORMAT = (
    "worst[ext=mp4][format_id!=download][format_id!=download_addr]/"
    f"{_YTDLP_VIDEO_FORMAT}"
)


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


def explain_video_skip(
    meta: LinkMetadata,
    settings: Settings,
    *,
    enforce_duration_limit: bool = True,
) -> str | None:
    if not settings.video_append_enabled:
        return "video append disabled"
    if meta.platform not in settings.allowed_video_platforms:
        return f"platform not allowed: {meta.platform}"
    if meta.media_type != MediaType.VIDEO:
        return f"not a video: media_type={meta.media_type}"
    if enforce_duration_limit and meta.duration_seconds is None:
        return "video duration unknown"
    if enforce_duration_limit and meta.duration_seconds > settings.max_video_duration_seconds:
        return (
            "video duration exceeds limit: "
            f"duration={meta.duration_seconds} limit={settings.max_video_duration_seconds}"
        )
    cookie_file = settings.cookie_file_for_platform(meta.platform)
    if meta.requires_auth and not cookie_file:
        logger.warning(
            "skip download: url=%s needs auth but no cookie file configured",
            meta.source_url,
        )
        return "platform requires auth"
    if not meta.download_candidates:
        return "no download candidate"
    return None


def _format_for_platform(platform: str) -> str:
    if platform == "tiktok":
        return _TIKTOK_YTDLP_VIDEO_FORMAT
    return _YTDLP_VIDEO_FORMAT


async def download_video(
    meta: LinkMetadata,
    settings: Settings,
    *,
    enforce_duration_limit: bool = True,
) -> DownloadedVideo:
    skip_reason = explain_video_skip(
        meta,
        settings,
        enforce_duration_limit=enforce_duration_limit,
    )
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

        options: dict[str, Any] = {
            "format": _format_for_platform(meta.platform),
            "merge_output_format": "mp4",
            "noplaylist": True,
            "outtmpl": str(temp_dir / "%(title).80s-%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "socket_timeout": 30,
            "logger": _YtDlpLogger(),
        }
        apply_ytdlp_runtime(options, meta.platform)

        def do_download(opts: dict[str, Any]) -> set[Path]:
            before = set(temp_dir.iterdir())
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([source_url])
            except Exception as e:
                raise VideoDownloadError(f"yt-dlp download failed: {e}") from e
            return before

        cookie_file = settings.cookie_file_for_platform(meta.platform)
        if cookie_file:
            with temporary_cookie_file(cookie_file) as temp_cookie_file:
                if temp_cookie_file:
                    options["cookiefile"] = temp_cookie_file
                before = do_download(options)
        else:
            before = do_download(options)

        after = [p for p in temp_dir.iterdir() if p.is_file() and p not in before]
        if not after:
            raise VideoDownloadError("download finished without output file")
        return max(after, key=lambda p: p.stat().st_size)

    loop = asyncio.get_running_loop()
    try:
        path = await loop.run_in_executor(None, _download)
        path = await loop.run_in_executor(
            None,
            lambda: _make_feishu_mp4(
                path,
                temp_dir,
                meta.duration_seconds,
                settings.max_video_file_mb,
            ),
        )
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


def _make_feishu_mp4(
    path: Path,
    temp_dir: Path,
    duration_seconds: int | None = None,
    max_file_mb: int | None = None,
) -> Path:
    output = temp_dir / f"{path.stem}.feishu.mp4"
    target_bitrates = _target_transcode_bitrates(duration_seconds, max_file_mb)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-vf",
        "scale=min(640\\,iw):-2",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
    ]
    if target_bitrates:
        video_kbps, audio_kbps = target_bitrates
        cmd.extend([
            "-b:v",
            f"{video_kbps}k",
            "-maxrate",
            f"{max(video_kbps, int(video_kbps * 1.2))}k",
            "-bufsize",
            f"{max(160, video_kbps * 2)}k",
            "-b:a",
            f"{audio_kbps}k",
        ])
    else:
        cmd.extend(["-crf", "23", "-b:a", "128k"])
    cmd.append(str(output))
    timeout = _transcode_timeout_seconds(duration_seconds)
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        stderr = getattr(e, "stderr", "") or str(e)
        raise VideoDownloadError(f"ffmpeg transcode failed: {stderr[:500]}") from e
    if not output.exists() or output.stat().st_size == 0:
        raise VideoDownloadError("ffmpeg transcode produced empty output")
    return output


def _target_transcode_bitrates(
    duration_seconds: int | None,
    max_file_mb: int | None,
) -> tuple[int, int] | None:
    if not duration_seconds or duration_seconds <= 0 or not max_file_mb or max_file_mb <= 0:
        return None

    target_total_kbps = int(max_file_mb * 1024 * 1024 * 8 * 0.9 / duration_seconds / 1000)
    audio_kbps = 64 if target_total_kbps >= 220 else 48
    if target_total_kbps < 120:
        audio_kbps = 32
    video_kbps = min(1200, max(64, target_total_kbps - audio_kbps - 12))
    return video_kbps, audio_kbps


def _transcode_timeout_seconds(duration_seconds: int | None) -> int:
    if duration_seconds is None or duration_seconds <= 0:
        return 180
    return max(180, min(7200, duration_seconds * 2 + 60))


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
