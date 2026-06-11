from __future__ import annotations

import shutil
from typing import Any

_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _ytdlp_http_headers(platform: str) -> dict[str, str]:
    if platform == "bilibili":
        return {
            "User-Agent": _BROWSER_USER_AGENT,
            "Referer": "https://www.bilibili.com/",
        }
    return {}


def _ytdlp_js_runtimes() -> dict[str, dict[str, str]]:
    deno_path = shutil.which("deno")
    if deno_path:
        return {"deno": {"path": deno_path}}
    return {"deno": {}}


def apply_ytdlp_runtime(options: dict[str, Any], platform: str) -> None:
    """Set js_runtimes and any platform-specific http_headers on a yt-dlp options dict."""
    options["js_runtimes"] = _ytdlp_js_runtimes()
    headers = _ytdlp_http_headers(platform)
    if headers:
        options["http_headers"] = headers
