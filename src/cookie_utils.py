from __future__ import annotations

import logging
import shutil
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class CookieError(Exception):
    """Raised when cookie file is missing, malformed, or contains no auth tokens."""


@contextmanager
def temporary_cookie_file(cookie_file: str) -> Iterator[str]:
    if not cookie_file:
        yield ""
        return

    source = Path(cookie_file)
    if not source.exists():
        yield ""
        return

    with tempfile.TemporaryDirectory(prefix="feishu-link-cookies-") as temp_dir:
        target = Path(temp_dir) / source.name
        shutil.copy2(source, target)
        yield str(target)


def get_cookie_header(cookie_file: str, domain: str) -> str:
    """Read a Netscape format cookie file and return a Cookie header for the domain."""
    if not cookie_file:
        return ""

    path = Path(cookie_file)
    if not path.exists():
        return ""

    jar = MozillaCookieJar(str(path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception as exc:
        logger.warning("Failed to parse cookie file: %s", exc)
        return ""

    pairs: list[str] = []
    for cookie in jar:
        cookie_domain = cookie.domain.lstrip(".")
        if cookie_domain == domain or domain.endswith(f".{cookie_domain}"):
            pairs.append(f"{cookie.name}={cookie.value}")

    return "; ".join(pairs)


def playwright_cookies_from_file(cookie_file: str, domain: str) -> list[dict[str, Any]]:
    """Convert a Netscape cookie file into Playwright's add_cookies() format.

    A freshly created persistent profile carries no login state, so any browser
    path that needs one has to seed it from the exported cookies.
    """
    if not cookie_file:
        return []

    path = Path(cookie_file)
    if not path.exists():
        return []

    jar = MozillaCookieJar(str(path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception as exc:
        logger.warning("Failed to parse cookie file for browser seeding: %s", exc)
        return []

    now = int(time.time())
    cookies: list[dict[str, Any]] = []
    for cookie in jar:
        cookie_domain = cookie.domain.lstrip(".")
        if cookie_domain != domain and not domain.endswith(f".{cookie_domain}"):
            continue
        if cookie.expires is not None and cookie.expires <= now:
            continue

        item: dict[str, Any] = {
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain or domain,
            "path": cookie.path or "/",
            "secure": bool(cookie.secure),
            "httpOnly": bool(cookie.has_nonstandard_attr("HttpOnly")),
        }
        if cookie.expires is not None:
            item["expires"] = cookie.expires
        cookies.append(item)

    return cookies


def cookie_value(cookie_header: str, name: str) -> str:
    """Extract a single cookie value from a Cookie header string."""
    for part in cookie_header.split("; "):
        if part.startswith(f"{name}="):
            return part.split("=", 1)[1]
    return ""
