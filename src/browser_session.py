from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Disable the on-disk HTTP cache so persistent profiles used only for cookie
# refresh / browser-backed requests do not grow unbounded over time.
_LAUNCH_ARGS = [
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--no-sandbox",
    "--disk-cache-size=0",
]

# Lock files Chromium writes into a persistent profile. A crashed run leaves them
# behind and blocks the next launch, so they are cleared before each launch.
_SINGLETON_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")

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


@asynccontextmanager
async def persistent_context(
    profile_dir: str,
    *,
    headless: bool = True,
    user_agent: str | None = None,
    timeout_ms: int = 60000,
    viewport: dict[str, int] | None = None,
) -> AsyncIterator[Any]:
    """Launch a persistent Chromium profile and yield its browser context.

    Serializes access per profile directory, clears stale singleton locks before
    launch, and guarantees the context is closed on exit. Playwright runtime
    errors propagate to the caller; a missing Playwright install raises
    BrowserUnavailableError.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise BrowserUnavailableError(
            "Playwright is not installed; rebuild the Docker image or run uv sync."
        ) from exc

    path = Path(profile_dir)
    path.mkdir(parents=True, exist_ok=True)
    _clear_singleton_locks(path)

    launch_kwargs: dict[str, Any] = {
        "user_data_dir": str(path),
        "headless": headless,
        "args": _LAUNCH_ARGS,
        "timeout": timeout_ms,
    }
    if user_agent:
        launch_kwargs["user_agent"] = user_agent
    if viewport:
        launch_kwargs["viewport"] = viewport

    async with _lock_for(path), async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(**launch_kwargs)
        try:
            yield context
        finally:
            await context.close()
