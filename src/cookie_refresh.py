from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Any

from .browser_session import BrowserUnavailableError, persistent_context
from .config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RefreshProfile:
    """How to refresh one platform's cookies from a persistent browser profile."""

    site_url: str
    cookie_domain: str
    required_names: frozenset[str]


# Platforms whose exported cookies expire and can be refreshed from a logged-in
# persistent Chromium profile. SESSDATA / auth_token are the session anchors used
# both to detect "logged in" and to judge staleness.
_PROFILES: dict[str, RefreshProfile] = {
    "bilibili": RefreshProfile(
        site_url="https://www.bilibili.com/",
        cookie_domain="bilibili.com",
        required_names=frozenset({"SESSDATA"}),
    ),
    "x": RefreshProfile(
        site_url="https://x.com/home",
        cookie_domain="x.com",
        required_names=frozenset({"auth_token", "ct0"}),
    ),
    "instagram": RefreshProfile(
        site_url="https://www.instagram.com/",
        cookie_domain="instagram.com",
        required_names=frozenset({"sessionid"}),
    ),
    "youtube": RefreshProfile(
        site_url="https://www.youtube.com/",
        cookie_domain="youtube.com",
        required_names=frozenset({"SAPISID", "__Secure-3PSID"}),
    ),
}

# Per-process throttle so a burst of stale-cookie requests does not launch the
# browser repeatedly. Keyed by platform, monotonic timestamps.
_last_refresh: dict[str, float] = {}

# Separate throttle for the reactive (failure-driven) refresh path so a burst of
# failed requests does not stampede the (seconds-costly) Chrome extraction.
_last_force: dict[str, float] = {}

# How long --browser-login waits for the human to finish logging in.
_LOGIN_WAIT_SECONDS = 300
_LOGIN_POLL_SECONDS = 2


def _domain_matches(cookie_domain: str, want: str) -> bool:
    d = cookie_domain.lstrip(".")
    return d == want or d.endswith(f".{want}")


def _resolve_target(platform: str, settings: Settings) -> str | None:
    """Resolve the per-platform cookie file to refresh into.

    Never returns the shared unified cookie_file: writing refreshed cookies there
    would clobber other platforms. If a conventional cookies/{platform}.txt file
    exists, prefer it over the shared file so refreshed cookies are also the ones
    used by yt-dlp.
    """
    configured = settings.platform_cookie_files.get(platform, "")
    if configured:
        return configured
    for cookie_dir in (Path("/etc/feishu-link/cookies"), Path("cookies")):
        candidate = cookie_dir / f"{platform}.txt"
        if candidate.exists():
            return str(candidate)
    if settings.cookie_file and Path(settings.cookie_file).exists():
        logger.warning(
            "cookie_refresh for %s needs a platform_cookie_files entry; the shared "
            "cookie_file would otherwise shadow the refreshed per-platform file",
            platform,
        )
        return None
    return str(Path("cookies") / f"{platform}.txt")


def cookie_is_stale(
    cookie_file: str,
    platform: str,
    stale_before_seconds: int,
    max_age_seconds: int | None = None,
) -> bool:
    """True if the platform's required cookies are missing or near expiry.

    ``max_age_seconds`` is an auxiliary trigger: even when the nominal expiry is
    far off (YouTube auth cookies nominally last ~2 years yet get rotated
    server-side well before that), a cookie file older than this many seconds is
    treated as stale so the pre-emptive refresh has a chance to swap in a fresh
    session before YouTube rotates it. ``None``/``0`` disables this trigger.
    """
    profile = _PROFILES.get(platform)
    if profile is None:
        return False

    path = Path(cookie_file)
    if not path.exists():
        return True

    if max_age_seconds:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return True
        if age >= max_age_seconds:
            return True

    jar = MozillaCookieJar(str(path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception as exc:
        logger.warning("Cannot parse %s for staleness: %s", cookie_file, exc)
        return True

    expiries: dict[str, int | None] = {}
    for cookie in jar:
        if cookie.name in profile.required_names and _domain_matches(
            cookie.domain, profile.cookie_domain
        ):
            expiries[cookie.name] = cookie.expires

    if not profile.required_names <= set(expiries):
        return True

    threshold = time.time() + stale_before_seconds
    return any(exp is not None and exp <= threshold for exp in expiries.values())


def write_netscape(cookies: list[dict[str, Any]], path: str) -> None:
    """Atomically write Playwright cookies as a Netscape cookie file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Netscape HTTP Cookie File",
        "# Auto-generated by feishu-link cookie refresh; do not edit.",
        "",
    ]
    for cookie in cookies:
        domain = str(cookie.get("domain", ""))
        name = str(cookie.get("name", ""))
        if not domain or not name:
            continue
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        cpath = str(cookie.get("path") or "/")
        secure = "TRUE" if cookie.get("secure") else "FALSE"
        expires = cookie.get("expires")
        expiry = int(expires) if isinstance(expires, int | float) and expires > 0 else 0
        prefix = "#HttpOnly_" if cookie.get("httpOnly") else ""
        value = str(cookie.get("value", ""))
        lines.append(
            f"{prefix}{domain}\t{include_sub}\t{cpath}\t{secure}\t{expiry}\t{name}\t{value}"
        )

    content = "\n".join(lines) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".cookies-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _platform_cookies(
    raw: list[dict[str, Any]],
    profile: RefreshProfile,
) -> list[dict[str, Any]]:
    return [c for c in raw if _domain_matches(str(c.get("domain", "")), profile.cookie_domain)]


def _has_required_cookies(
    cookies: list[dict[str, Any]],
    profile: RefreshProfile,
) -> bool:
    return profile.required_names <= {str(c.get("name", "")) for c in cookies}


def _cookiejar_cookie_to_dict(cookie: Any) -> dict[str, Any]:
    rest = getattr(cookie, "_rest", None) or {}
    http_only = any(str(key).lower() == "httponly" for key in rest)
    return {
        "domain": cookie.domain,
        "name": cookie.name,
        "value": cookie.value or "",
        "path": cookie.path or "/",
        "secure": bool(cookie.secure),
        "expires": cookie.expires if cookie.expires else -1,
        "httpOnly": http_only,
    }


def _extract_chrome_cookies_sync(chrome_profile: str) -> list[dict[str, Any]]:
    """Read the system Chrome's cookie store (blocking: sqlite copy + keychain)."""
    from yt_dlp.cookies import extract_cookies_from_browser

    jar = extract_cookies_from_browser("chrome", chrome_profile or None)
    return [_cookiejar_cookie_to_dict(cookie) for cookie in jar]


async def _refresh_from_chrome(
    platform: str,
    profile: RefreshProfile,
    settings: Settings,
    target: str,
) -> bool:
    try:
        raw = await asyncio.to_thread(
            _extract_chrome_cookies_sync, settings.cookie_refresh_chrome_profile
        )
    except Exception as exc:
        logger.warning("Chrome cookie extraction for %s failed: %s", platform, exc)
        return False

    cookies = _platform_cookies(raw, profile)
    if not _has_required_cookies(cookies, profile):
        logger.warning(
            "Chrome has no logged-in %s session; open Chrome on this machine and log in at %s",
            platform,
            profile.site_url,
        )
        return False

    write_netscape(cookies, target)
    logger.info("Refreshed %d cookies for %s from Chrome -> %s", len(cookies), platform, target)
    return True


async def refresh_cookies(
    platform: str,
    settings: Settings,
    *,
    target: str | None = None,
) -> bool:
    """Export fresh cookies for the platform from the configured source."""
    profile = _PROFILES.get(platform)
    if profile is None:
        logger.warning("No cookie-refresh profile for platform %s", platform)
        return False

    if target is None:
        target = _resolve_target(platform, settings)
    if not target:
        return False

    if settings.cookie_refresh_source == "chrome":
        return await _refresh_from_chrome(platform, profile, settings, target)

    profile_dir = str(Path(settings.cookie_refresh_profile_dir) / platform)
    timeout_ms = int(settings.cookie_refresh_browser_timeout * 1000)
    try:
        async with persistent_context(
            profile_dir,
            headless=settings.cookie_refresh_browser_headless,
            timeout_ms=timeout_ms,
            channel=settings.cookie_refresh_browser_channel or None,
        ) as context:
            # Refresh runs only when the exported cookie is missing or near
            # expiry, so always load the site first: exporting without a page
            # load would just re-export the same expiring cookie, while a visit
            # lets the site rotate/renew the session.
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(
                profile.site_url,
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            cookies = _platform_cookies(await context.cookies(), profile)
    except BrowserUnavailableError as exc:
        logger.warning("Cookie refresh for %s unavailable: %s", platform, exc)
        return False
    except Exception as exc:
        logger.warning("Cookie refresh for %s failed: %s", platform, exc)
        return False

    if not _has_required_cookies(cookies, profile):
        logger.warning(
            "Cookie refresh for %s produced no logged-in session; run "
            "`python main.py --browser-login %s`",
            platform,
            platform,
        )
        return False

    write_netscape(cookies, target)
    logger.info("Refreshed %d cookies for %s -> %s", len(cookies), platform, target)
    return True


async def ensure_fresh_cookies(platform: str, settings: Settings) -> None:
    """Refresh the platform's cookies before use if enabled, stale, and not throttled.

    No-op unless cookie_refresh is enabled for the platform. Refresh failures are
    logged and swallowed so the caller proceeds with whatever cookies exist.
    """
    if not settings.cookie_refresh_enabled:
        return
    if platform not in settings.cookie_refresh_platforms or platform not in _PROFILES:
        return

    target = _resolve_target(platform, settings)
    if not target:
        return
    if not cookie_is_stale(
        target,
        platform,
        settings.cookie_refresh_stale_before_seconds,
        settings.cookie_refresh_max_age_seconds,
    ):
        return

    now = time.monotonic()
    last = _last_refresh.get(platform)
    if last is not None and now - last < settings.cookie_refresh_min_interval_seconds:
        return
    _last_refresh[platform] = now

    await refresh_cookies(platform, settings, target=target)


async def force_refresh(platform: str, settings: Settings) -> bool:
    """Reactively re-export cookies after a runtime "cookies invalid" signal.

    Unlike ``ensure_fresh_cookies`` this ignores nominal-expiry staleness (the
    whole point is that the cookies look valid by expiry yet were rotated). A
    short per-platform cooldown still guards against a burst of failed requests
    stampeding the Chrome extraction. Returns True only if fresh cookies were
    written; failures are logged and swallowed by ``refresh_cookies``.
    """
    if not settings.cookie_refresh_enabled:
        return False
    if platform not in settings.cookie_refresh_platforms or platform not in _PROFILES:
        return False

    target = _resolve_target(platform, settings)
    if not target:
        return False

    now = time.monotonic()
    last = _last_force.get(platform)
    if last is not None and now - last < settings.cookie_refresh_reactive_cooldown_seconds:
        return False
    _last_force[platform] = now

    return await refresh_cookies(platform, settings, target=target)


def _is_closed_error(exc: Exception) -> bool:
    """Playwright raises TargetClosedError-family errors when the user closes
    the login window mid-wait; match on message to keep the import lazy."""
    return "has been closed" in str(exc)


async def _wait_for_login(context: Any, profile: RefreshProfile) -> list[dict[str, Any]]:
    deadline = time.monotonic() + _LOGIN_WAIT_SECONDS
    while time.monotonic() < deadline:
        cookies = _platform_cookies(await context.cookies(), profile)
        if _has_required_cookies(cookies, profile):
            return cookies
        await asyncio.sleep(_LOGIN_POLL_SECONDS)
    return []


async def browser_login(platform: str, settings: Settings) -> bool:
    """Open a headed persistent profile so the user can log in once.

    Waits for the platform's session cookies to appear, then exports them to the
    per-platform cookie file. The logged-in profile persists for later refreshes.
    """
    profile = _PROFILES.get(platform)
    if profile is None:
        logger.error("No cookie-refresh profile for platform %s", platform)
        return False

    if settings.cookie_refresh_source == "chrome":
        logger.info(
            "cookie_refresh_source is 'chrome': refresh reads the system Chrome, "
            "not the profile this login seeds; log in to %s in Chrome instead "
            "unless you plan to switch to source 'browser_profile'",
            profile.site_url,
        )

    target = _resolve_target(platform, settings)
    profile_dir = str(Path(settings.cookie_refresh_profile_dir) / platform)
    timeout_ms = int(settings.cookie_refresh_browser_timeout * 1000)
    try:
        async with persistent_context(
            profile_dir,
            headless=False,
            timeout_ms=timeout_ms,
            channel=settings.cookie_refresh_browser_channel or None,
        ) as context:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(profile.site_url, wait_until="domcontentloaded", timeout=timeout_ms)
            logger.info(
                "Log in to %s in the opened window; waiting up to %ds for session cookies",
                platform,
                _LOGIN_WAIT_SECONDS,
            )
            cookies = await _wait_for_login(context, profile)
    except BrowserUnavailableError as exc:
        logger.error("Cannot open browser for login: %s", exc)
        return False
    except Exception as exc:
        if _is_closed_error(exc):
            logger.warning(
                "Login window for %s was closed before session cookies appeared; "
                "run `python main.py --browser-login %s` again and complete the login",
                platform,
                platform,
            )
        else:
            logger.error("Browser login for %s failed: %s", platform, exc)
        return False

    if not cookies:
        logger.warning("No %s session cookies detected before timeout", platform)
        return False
    if target:
        write_netscape(cookies, target)
        logger.info("Saved %d %s cookies to %s", len(cookies), platform, target)
    else:
        logger.info(
            "%s profile is logged in; set platform_cookie_files[%s] to export its cookies",
            platform,
            platform,
        )
    return True
