"""Bounded, isolated Chrome fallback for social content cards."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import weakref
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from ..browser_session import BrowserUnavailableError, persistent_context
from ..config import Settings
from ..cookie_utils import playwright_cookies_from_file
from ..tiktok_comments import chrome_user_agent
from .base import LinkMetadata, ParserError
from .social_page import page_identity, parse_page_metadata

__all__ = ["SocialBrowserParser", "parse_page_metadata"]

logger = logging.getLogger(__name__)
_CLEANUP_RESERVE = 5.0
_CLOSE_TIMEOUT = 2.0
_POLL_SECONDS = 0.4
_MAX_RESPONSE_BYTES = 3_000_000
_MAX_PAYLOADS = 16
_COOKIE_DOMAINS = {
    "tiktok": ("tiktok.com",),
    "douyin": ("douyin.com", "iesdouyin.com"),
    "instagram": ("instagram.com",),
    "youtube": ("youtube.com",),
    "x": ("x.com", "twitter.com"),
}
_RESPONSE_PATHS = {
    "tiktok": ("/api/item/detail",),
    "douyin": ("/aweme/v1/web/aweme/detail/",),
    "instagram": ("/graphql/query", "/api/graphql", "/api/v1/media/"),
    "youtube": ("/youtubei/v1/player", "/youtubei/v1/next"),
    "x": ("/graphql/",),
}
# Semaphores belong to an event loop and are shared across parser instances.
_LIMITS: weakref.WeakKeyDictionary[Any, dict[str, asyncio.Semaphore]] = weakref.WeakKeyDictionary()


def _limits(platform: str, settings: Settings) -> tuple[asyncio.Semaphore, asyncio.Semaphore]:
    loop = asyncio.get_running_loop()
    limits = _LIMITS.setdefault(loop, {})
    global_limit = max(1, min(settings.card_browser_concurrency, 2))
    platform_limit = max(1, min(settings.card_browser_platform_concurrency, 1))
    global_sem = limits.setdefault("global", asyncio.Semaphore(global_limit))
    platform_sem = limits.setdefault(platform, asyncio.Semaphore(platform_limit))
    return global_sem, platform_sem


def _response_allowed(response_url: str, platform: str) -> bool:
    parsed = urlsplit(response_url)
    host = parsed.hostname or ""
    if not any(host == d or host.endswith(f".{d}") for d in _COOKIE_DOMAINS[platform]):
        return False
    path = parsed.path.lower()
    if any(word in path for word in ("comment", "related", "recommend", "download")):
        return False
    if platform == "x" and not any(name in path for name in ("tweetdetail", "tweetresult")):
        return False
    return any(marker in path for marker in _RESPONSE_PATHS[platform])


class SocialBrowserParser:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def parse(self, url: str) -> LinkMetadata:
        platform, _, _ = page_identity(url)
        if platform not in _COOKIE_DOMAINS:
            raise ParserError(url, "unsupported: no social browser parser for this platform")
        budget = min(30.0, max(0.01, self._settings.card_browser_timeout))
        started = time.monotonic()
        global_sem, platform_sem = _limits(platform, self._settings)
        try:
            # Waiting for a slot consumes the same budget as page loading.
            async with asyncio.timeout(budget), platform_sem, global_sem:
                return await self._collect(url, platform, started + budget)
        except TimeoutError as exc:
            raise ParserError(url, "timeout: social browser card deadline exceeded") from exc
        except BrowserUnavailableError as exc:
            raise ParserError(url, "browser_unavailable: Chrome could not start") from exc
        except ParserError:
            raise
        except Exception as exc:
            # Playwright exceptions may include page source/request URLs. Log only their class.
            logger.warning(
                "social browser failed: platform=%s error=%s", platform, type(exc).__name__
            )
            raise ParserError(url, f"browser_error: {type(exc).__name__}") from exc

    async def _collect(self, url: str, platform: str, deadline: float) -> LinkMetadata:
        settings = self._settings
        profile = Path(settings.card_browser_profile_dir) / platform
        remaining = max(0.01, deadline - time.monotonic())
        work_budget = max(0.01, remaining - min(_CLEANUP_RESERVE, remaining / 3))
        payloads: list[Any] = []
        response_tasks: set[asyncio.Task[None]] = set()
        last_error = "no_content: target post has not rendered"

        # Chrome version lookup is local; it must not block the asyncio loop.
        async with asyncio.timeout(min(2.0, work_budget)):
            user_agent = await asyncio.to_thread(chrome_user_agent)
        async with persistent_context(
            str(profile),
            headless=False,
            user_agent=user_agent,
            channel=settings.card_browser_channel or "chrome",
            extra_args=["--headless=new", "--no-first-run", "--no-default-browser-check"]
            if settings.card_browser_headless
            else ["--no-first-run", "--no-default-browser-check"],
            omit_args=("--disable-gpu",),
            viewport={"width": 1280, "height": 900},
            timeout_ms=max(1, int(work_budget * 1000)),
            proxy_server=settings.tiktok_comment_proxy or None if platform == "tiktok" else None,
            close_timeout_seconds=_CLOSE_TIMEOUT,
        ) as context:
            try:
                async with asyncio.timeout_at(deadline - min(_CLEANUP_RESERVE, remaining / 3)):
                    cookies = []
                    for domain in _COOKIE_DOMAINS[platform]:
                        cookies.extend(
                            playwright_cookies_from_file(
                                settings.cookie_file_for_platform(platform), domain
                            )
                        )
                    if cookies:
                        await context.add_cookies(cookies)
                    page = context.pages[0] if context.pages else await context.new_page()
                    locked_url = url if page_identity(url)[1] else ""

                    def lock_target(candidate_url: str) -> None:
                        nonlocal locked_url
                        candidate_platform, candidate_id, _ = page_identity(candidate_url)
                        if not locked_url and candidate_platform == platform and candidate_id:
                            parsed = urlsplit(candidate_url)
                            # Share redirects carry tracking identifiers unnecessary for cards.
                            query = urlencode(
                                [
                                    (key, value)
                                    for key, value in parse_qsl(parsed.query)
                                    if key in {"modal_id", "img_index"}
                                ]
                            )
                            locked_url = urlunsplit(
                                (parsed.scheme, parsed.netloc, parsed.path, query, "")
                            )

                    def on_frame_navigated(frame: Any) -> None:
                        if frame == page.main_frame:
                            lock_target(frame.url)

                    def lock_navigation_response(response: Any) -> None:
                        request = getattr(response, "request", None)
                        if (
                            request is None
                            or not request.is_navigation_request()
                            or request.frame != page.main_frame
                        ):
                            return
                        # Inspect the original redirect chain before its final response.
                        # A short link may land on an unavailable note and then a recommendation.
                        chain = []
                        while request is not None and len(chain) < 20:
                            chain.append(request.url)
                            request = request.redirected_from
                        for candidate_url in reversed(chain):
                            lock_target(candidate_url)
                        lock_target(response.url)
                        location = response.headers.get("location", "")
                        if location:
                            lock_target(urljoin(response.url, location))

                    async def block_media(route: Any, request: Any) -> None:
                        # The card needs image URLs, not image/video response bodies.
                        if request.resource_type in {"image", "media", "font"}:
                            await route.abort()
                        else:
                            await route.continue_()

                    async def read_response(response: Any) -> None:
                        if len(payloads) >= _MAX_PAYLOADS:
                            return
                        try:
                            headers = response.headers
                            size = int(headers.get("content-length", "0"))
                            if size > _MAX_RESPONSE_BYTES:
                                return
                            text = await response.text()
                            if text and len(text) <= _MAX_RESPONSE_BYTES:
                                payloads.append(json.loads(text))
                        except (ValueError, TypeError):
                            pass
                        except Exception as exc:
                            logger.debug("card response unreadable: error=%s", type(exc).__name__)

                    def on_response(response: Any) -> None:
                        lock_navigation_response(response)
                        if _response_allowed(response.url, platform) and len(response_tasks) < 6:
                            task = asyncio.create_task(read_response(response))
                            response_tasks.add(task)
                            task.add_done_callback(response_tasks.discard)

                    await page.route("**/*", block_media)
                    page.on("response", on_response)
                    page.on("framenavigated", on_frame_navigated)
                    try:
                        response = await page.goto(
                            url,
                            wait_until="domcontentloaded",
                            timeout=max(1, int(work_budget * 1000)),
                        )
                        if response is not None:
                            lock_navigation_response(response)
                        lock_target(page.url)
                        status = getattr(response, "status", None)
                        if status in {401, 403, 404, 429}:
                            reason = {401: "auth", 403: "auth", 404: "deleted", 429: "rate_limit"}
                            raise ParserError(url, f"{reason[status]}: page HTTP {status}")
                        best: LinkMetadata | None = None
                        while True:
                            try:
                                html = await page.content()
                            except Exception as exc:
                                # Douyin note/share pages navigate again after DOMContentLoaded.
                                # A snapshot racing that navigation is not a content failure.
                                if not any(
                                    message in str(exc).lower()
                                    for message in (
                                        "page is navigating",
                                        "execution context was destroyed",
                                    )
                                ):
                                    raise
                                await asyncio.sleep(_POLL_SECONDS)
                                continue
                            try:
                                current = parse_page_metadata(
                                    locked_url or url, html, final_url=page.url, payloads=payloads
                                )
                            except ParserError as exc:
                                last_error = exc.reason
                                if any(
                                    marker in last_error
                                    for marker in ("auth:", "challenge:", "target_mismatch:")
                                ):
                                    raise ParserError(url, last_error) from exc
                            else:
                                current.source_url = url
                                best = current
                                if current.cover_url or current.has_visual is False:
                                    return current
                            if deadline - time.monotonic() < _CLEANUP_RESERVE + 1:
                                if best is not None:
                                    return best
                                raise ParserError(url, last_error)
                            await asyncio.sleep(_POLL_SECONDS)
                    finally:
                        page.remove_listener("response", on_response)
                        page.remove_listener("framenavigated", on_frame_navigated)
            finally:
                for task in response_tasks:
                    task.cancel()
                if response_tasks:
                    with contextlib.suppress(TimeoutError):
                        async with asyncio.timeout(0.5):
                            await asyncio.gather(*response_tasks, return_exceptions=True)
