"""Isolated metadata extraction; stdin/stdout carry only the worker protocol."""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict
from typing import Any

from ..ytdlp_options import YtDlpSignalLogger, apply_ytdlp_runtime
from .ytdlp import _metadata_from_info

logger = logging.getLogger(__name__)


def metadata_options(platform: str, cookie_file: str) -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": False,
        "skip_download": True,
        "noplaylist": True,
        "noprogress": True,
        "ignore_no_formats_error": True,
        "socket_timeout": 15,
        "extractor_retries": 1,
        "retries": 1,
        "source_address": "0.0.0.0",
        "logger": YtDlpSignalLogger(logger),
    }
    apply_ytdlp_runtime(options, platform)
    if cookie_file:
        options["cookiefile"] = cookie_file
    return options


def main() -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        import yt_dlp

        payload = json.load(sys.stdin)
        url, platform = payload["url"], payload["platform"]
        with yt_dlp.YoutubeDL(metadata_options(platform, payload.get("cookie_file", ""))) as ydl:
            info = ydl.extract_info(url, download=False)
        if not isinstance(info, dict):
            raise ValueError("yt-dlp returned invalid metadata")
        meta = _metadata_from_info(url, platform, info)
        meta.content_verified = True
        meta.has_visual = bool(meta.cover_url) if meta.media_type != "video" else True
        meta.cover_candidates = list(dict.fromkeys(
            [meta.cover_url] + [str(item.get("url")) for item in info.get("thumbnails") or []
                                if isinstance(item, dict) and item.get("url")]
        ))[:3]
        sys.stdout.write(json.dumps(asdict(meta), default=str, ensure_ascii=False))
        return 0
    except Exception as exc:
        logger.error("%s: %s", type(exc).__name__, str(exc)[:600])
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
