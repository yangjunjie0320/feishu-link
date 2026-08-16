from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Disable the on-disk HTTP cache so persistent profiles used only for cookie
# refresh / browser-backed requests do not grow unbounded over time.
# AutomationControlled is disabled so pages do not see navigator.webdriver;
# Google refuses logins from browsers that expose it.
_LAUNCH_ARGS = [
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--no-sandbox",
    "--disk-cache-size=0",
    "--disable-blink-features=AutomationControlled",
]

# Lock files Chromium writes into a persistent profile. A crashed run leaves them
# behind and blocks the next launch, so they are cleared before each launch.
_SINGLETON_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")

# A context that never closes cleanly (agent SIGKILLed, Playwright driver lost)
# leaves Chromium running forever; production accumulated five such processes
# alive for 21 days. Reaped before launch, and close() is capped so a hung
# teardown cannot create the next one.
_CLOSE_TIMEOUT_SECONDS = 15.0
_ORPHAN_TERM_GRACE_SECONDS = 2.0

_locks: dict[str, asyncio.Lock] = {}


class BrowserUnavailableError(RuntimeError):
    """Raised when Playwright is not installed or a browser cannot be launched."""


def _lock_for(profile_path: Path) -> asyncio.Lock:
    key = str(profile_path)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def _clear_singleton_locks(profile_path: Path) -> None:
    for name in _SINGLETON_FILES:
        try:
            (profile_path / name).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Failed to clear stale %s in %s: %s", name, profile_path, exc)


def _user_data_dir_markers(profile_path: Path) -> tuple[str, ...]:
    """The --user-data-dir spellings a process may have been launched with.

    launch_persistent_context receives str(profile_path) verbatim, so a relative
    config value shows up relative in ps output; resolve() covers the rest.
    """
    markers = {f"--user-data-dir={profile_path}"}
    with contextlib.suppress(OSError):
        markers.add(f"--user-data-dir={profile_path.resolve()}")
    return tuple(markers)


def _command_uses_profile(command: str, markers: tuple[str, ...]) -> bool:
    """Whole-token match only: browser-data/tiktok must not match tiktok-probe."""
    for marker in markers:
        index = command.find(marker)
        while index != -1:
            end = index + len(marker)
            if end == len(command) or command[end].isspace():
                return True
            index = command.find(marker, end)
    return False


def _orphan_pids(profile_path: Path) -> list[tuple[int, str]]:
    """Chromium processes for this profile that init has adopted.

    ppid == 1 is the only safe signal that the agent which launched them is gone:
    the per-profile asyncio.Lock serializes this process only, so a live parent
    may be another instance or a maintenance probe using the same profile.
    """
    markers = _user_data_dir_markers(profile_path)
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,etime=,command="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(
            "Failed to list processes while reaping orphans in %s: %s", profile_path, exc
        )
        return []

    orphans: list[tuple[int, str]] = []
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=3)
        if len(fields) < 4:
            continue
        pid_text, ppid_text, etime, command = fields
        if ppid_text != "1" or not _command_uses_profile(command, markers):
            continue
        try:
            orphans.append((int(pid_text), etime))
        except ValueError:
            continue
    return orphans


async def _reap_orphans(profile_path: Path) -> None:
    """SIGTERM then SIGKILL orphaned Chromium processes for this profile.

    Best effort: any failure is logged and ignored, never blocking the launch.
    """
    orphans = _orphan_pids(profile_path)
    if not orphans:
        return

    for pid, etime in orphans:
        logger.warning(
            "Reaping orphan Chromium pid=%d etime=%s profile=%s", pid, etime, profile_path
        )
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            logger.warning("SIGTERM failed for orphan Chromium pid=%d: %s", pid, exc)

    await asyncio.sleep(_ORPHAN_TERM_GRACE_SECONDS)

    for pid, _etime in orphans:
        try:
            os.kill(pid, 0)
        except OSError:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            logger.warning("Orphan Chromium pid=%d ignored SIGTERM, sent SIGKILL", pid)
        except OSError as exc:
            logger.warning("SIGKILL failed for orphan Chromium pid=%d: %s", pid, exc)


@asynccontextmanager
async def persistent_context(
    profile_dir: str,
    *,
    headless: bool = True,
    user_agent: str | None = None,
    timeout_ms: int = 60000,
    viewport: dict[str, int] | None = None,
    channel: str | None = None,
    extra_args: list[str] | None = None,
    omit_args: tuple[str, ...] = (),
) -> AsyncIterator[Any]:
    """Launch a persistent Chromium profile and yield its browser context.

    Serializes access per profile directory, reaps orphaned Chromium processes
    and clears stale singleton locks before launch, and guarantees the context is
    closed on exit. Playwright runtime errors propagate to the caller; a missing
    Playwright install raises BrowserUnavailableError.

    extra_args appends to the shared launch flags; omit_args drops some. Both
    exist for anti-bot-sensitive callers: TikTok needs Chrome's new headless
    mode (`headless=False` plus `--headless=new`, a full browser rather than the
    stripped-down build Playwright's own headless flag selects) and needs
    `--disable-gpu` dropped, since the missing WebGL fingerprint reads as
    automation. See DESIGN.md.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise BrowserUnavailableError(
            "Playwright is not installed; rebuild the Docker image or run uv sync."
        ) from exc

    path = Path(profile_dir)
    path.mkdir(parents=True, exist_ok=True)
    await _reap_orphans(path)
    _clear_singleton_locks(path)

    launch_kwargs: dict[str, Any] = {
        "user_data_dir": str(path),
        "headless": headless,
        "args": [a for a in _LAUNCH_ARGS if a not in omit_args] + list(extra_args or []),
        "timeout": timeout_ms,
    }
    if user_agent:
        launch_kwargs["user_agent"] = user_agent
    if viewport:
        launch_kwargs["viewport"] = viewport
    if channel:
        # Branded browser channel (e.g. "chrome"): Google rejects logins from
        # the bundled Chromium build, so profiles that need a Google session
        # must run on the system's real Chrome.
        launch_kwargs["channel"] = channel

    async with _lock_for(path), async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(**launch_kwargs)
        try:
            yield context
        finally:
            # A hung close() would otherwise leave Chromium behind and mask the
            # real error; swallowing it lets async_playwright().__aexit__ still
            # tear the driver down.
            try:
                await asyncio.wait_for(context.close(), timeout=_CLOSE_TIMEOUT_SECONDS)
            except TimeoutError:
                logger.warning(
                    "Browser context close timed out after %.0fs for %s",
                    _CLOSE_TIMEOUT_SECONDS,
                    path,
                )
            except Exception as exc:
                logger.warning("Browser context close failed for %s: %s", path, exc)
