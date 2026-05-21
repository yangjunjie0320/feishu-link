from __future__ import annotations

import logging
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from http.cookiejar import MozillaCookieJar
from pathlib import Path

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
