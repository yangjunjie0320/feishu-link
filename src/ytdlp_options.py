from __future__ import annotations

import logging
import shutil
from typing import Any

# yt-dlp runtime signals. YouTube rotates session cookies server-side as a
# security measure independent of their nominal expiry, so the only reliable
# "cookies are dead" signal is what yt-dlp actually reports at runtime. These
# substrings are matched case-insensitively against yt-dlp log/exception text.
COOKIE_INVALID_SIGNALS: tuple[str, ...] = (
    "no longer valid",
    "rotated in the browser",
    "sign in to confirm",
    "confirm you're not a bot",
)
RATE_LIMIT_SIGNALS: tuple[str, ...] = (
    "rate-limit",
    "rate limited",
    "this content isn't available",
)


def looks_like_cookie_invalidation(text: str) -> bool:
    lowered = text.lower()
    return any(signal in lowered for signal in COOKIE_INVALID_SIGNALS)


def looks_like_rate_limit(text: str) -> bool:
    lowered = text.lower()
    return any(signal in lowered for signal in RATE_LIMIT_SIGNALS)


class YtDlpSignalLogger:
    """A yt-dlp ``logger`` that forwards to a stdlib logger and records whether
    the run emitted a cookie-invalidation or rate-limit signal.

    yt-dlp surfaces "cookies no longer valid" via ``logger.warning`` (not the
    raised exception, which only carries the downstream rate-limit), so callers
    must inspect ``cookie_invalid`` after the run in addition to the exception
    text. A fresh instance is created per yt-dlp call.
    """

    def __init__(self, delegate: logging.Logger, prefix: str = "yt-dlp: ") -> None:
        self._delegate = delegate
        self._prefix = prefix
        self.cookie_invalid = False
        self.rate_limited = False

    def _scan(self, msg: str) -> None:
        if looks_like_cookie_invalidation(msg):
            self.cookie_invalid = True
        if looks_like_rate_limit(msg):
            self.rate_limited = True

    def debug(self, msg: str) -> None:
        self._scan(msg)
        self._delegate.debug("%s%s", self._prefix, msg)

    def info(self, msg: str) -> None:
        self._scan(msg)
        self._delegate.info("%s%s", self._prefix, msg)

    def warning(self, msg: str) -> None:
        self._scan(msg)
        self._delegate.warning("%s%s", self._prefix, msg)

    def error(self, msg: str) -> None:
        self._scan(msg)
        self._delegate.error("%s%s", self._prefix, msg)


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
