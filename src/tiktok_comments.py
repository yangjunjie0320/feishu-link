"""TikTok comment fetching through a Playwright page.

TikTok's web API is signed by the page's own webmssdk.js, which hooks fetch and
XHR to attach X-Bogus / X-Gnarly. Rather than reverse engineering the signature,
requests are issued from inside the loaded video page so the site signs them for
us. Everything above that boundary -- pagination, termination, normalization --
is plain logic that takes a fetcher callable, so only `_browser_json_fetcher`
touches Playwright.

Note the API answers HTTP 200 with an empty body for logged-out clients (its
generic content-throttling response), so a session cookie is required even
though signing works without one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlencode

import httpx

from .browser_session import BrowserUnavailableError, persistent_context
from .config import Settings
from .cookie_utils import playwright_cookies_from_file

logger = logging.getLogger(__name__)

JsonFetcher = Callable[[str], Awaitable[Any]]

_AWEME_ID_RE = re.compile(r"/(?:video|photo)/(\d+)")
_SHORT_HOSTS = ("vt.tiktok.com", "vm.tiktok.com")

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_VIEWPORT = {"width": 1280, "height": 900}

# Query parameters describing the "browser" making the request. They must agree
# with the user agent and viewport above; a mismatch reads as automation.
_CLIENT_PARAMS = {
    "aid": "1988",
    "app_name": "tiktok_web",
    "channel": "tiktok_web",
    "device_platform": "web_pc",
    "app_language": "en",
    "region": "US",
    "priority_region": "",
    "browser_language": "en-US",
    "browser_name": "Mozilla",
    "browser_platform": "MacIntel",
    "browser_online": "true",
    "cookie_enabled": "true",
    "screen_width": str(_VIEWPORT["width"]),
    "screen_height": str(_VIEWPORT["height"]),
    "os": "mac",
}

# Distinguishing a captcha wall from a plain login requirement matters: one is
# waited out or cleared by hand, the other needs fresh cookies.
_CAPTCHA_MARKERS = ("captcha", "verify_center", "secsdk-captcha")
_CAPTCHA_SELECTOR = "#captcha-verify-container, .captcha_verify_container"

# TikTok status_code values seen in the wild. 0 is success.
_STATUS_LOGIN_REQUIRED = frozenset({8, 2154})
_STATUS_ITEM_UNAVAILABLE = frozenset({2053, 10204})

# Leave room inside the caller's deadline for the browser to shut down; a
# cancellation that lands mid-close is exactly how orphan Chromium is created.
_CLOSE_MARGIN_SECONDS = 10.0
_DEADLINE_MARGIN_SECONDS = 5.0

_SIGNER_READY_JS = """() => {
    return typeof window.byted_acrawler !== "undefined"
        || (window.fetch && !/\\[native code\\]/.test(window.fetch.toString()));
}"""

_FETCH_JS = """async ({url}) => {
    let target = url;
    if (!target.includes("msToken=")) {
        const m = document.cookie.match(/(?:^|;\\s*)msToken=([^;]+)/);
        if (m) {
            target += "&msToken=" + m[1];
        }
    }
    try {
        const response = await fetch(target, {
            credentials: "include",
            headers: {"accept": "application/json"},
        });
        return {
            status: response.status,
            text: await response.text(),
            contentType: response.headers.get("content-type") || "",
        };
    } catch (e) {
        return {status: 0, text: "", contentType: "", error: String(e)};
    }
}"""


class TikTokCommentError(Exception):
    """Explainable TikTok comment fetch failure; the message is user-facing."""


def aweme_id_from_url(url: str) -> str:
    match = _AWEME_ID_RE.search(url)
    return match.group(1) if match else ""


def is_short_link(url: str) -> bool:
    return any(host in url for host in _SHORT_HOSTS)


async def resolve_tiktok_url(url: str, client: httpx.AsyncClient) -> str:
    """Expand a vt./vm. short link so the aweme id can be read off the path."""
    if not is_short_link(url):
        return url
    try:
        response = await client.head(
            url, follow_redirects=True, headers={"User-Agent": _USER_AGENT}
        )
    except httpx.HTTPError as e:
        logger.info("tiktok short link unresolved: url=%s error=%s", url, e)
        return url
    return str(response.url)


def build_comment_list_url(aweme_id: str, *, cursor: int, count: int) -> str:
    params = {
        "aweme_id": aweme_id,
        "cursor": str(cursor),
        "count": str(count),
        **_CLIENT_PARAMS,
    }
    return f"https://www.tiktok.com/api/comment/list/?{urlencode(params)}"


def normalize_tiktok_comment(raw: object) -> object:
    """Rewrite a TikTok comment into the keys `comments_from_raw` understands.

    The generic converter knows text/like_count/reply_count/id, none of which
    are TikTok's spellings. The `user` key is dropped so the Instagram branch in
    `_comment_from_raw` cannot claim it.
    """
    if not isinstance(raw, dict):
        return raw

    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    unique_id = str(user.get("unique_id") or "")
    nickname = str(user.get("nickname") or "")
    author = nickname or (f"@{unique_id}" if unique_id else "")
    # reply_id is "0" on root comments, which would otherwise read as a parent.
    parent = str(raw.get("reply_id") or raw.get("reply_to_reply_id") or "")
    if parent in ("0", ""):
        parent = ""

    return {
        "id": raw.get("cid"),
        "text": raw.get("text"),
        "author": author,
        "author_url": f"https://www.tiktok.com/@{unique_id}" if unique_id else "",
        # TikTok has no stable per-comment permalink.
        "comment_url": "",
        "like_count": raw.get("digg_count"),
        "reply_count": raw.get("reply_comment_total"),
        "parent": parent,
        "replies": [],
    }


def _captcha_marker(text: str) -> str:
    lowered = text.lower()
    for marker in _CAPTCHA_MARKERS:
        if marker in lowered:
            return marker
    return ""


async def paginate_comments(
    fetch_json: JsonFetcher,
    aweme_id: str,
    *,
    max_comments: int,
    page_size: int,
    max_pages: int,
    request_delay: float,
    deadline: float,
) -> tuple[list[object], int | None]:
    """Walk the comment cursor, returning normalized raw comments and the total.

    Errors on the first page raise; on later pages they log and return whatever
    was collected, since a partial sample still analyzes fine.
    """
    collected: list[object] = []
    seen_cids: set[str] = set()
    total_count: int | None = None
    cursor = 0

    for index in range(max_pages):
        payload = await fetch_json(build_comment_list_url(aweme_id, cursor=cursor, count=page_size))
        if not isinstance(payload, dict):
            raise TikTokCommentError("TikTok 评论接口返回了无法解析的数据。")

        status_code = payload.get("status_code")
        comments = payload.get("comments")
        comments = comments if isinstance(comments, list) else []
        if total_count is None and isinstance(payload.get("total"), int):
            total_count = payload["total"]

        if status_code not in (0, None):
            message = _status_code_message(int(status_code))
            if index == 0:
                raise TikTokCommentError(message)
            logger.warning(
                "tiktok comment page failed mid-pagination: aweme_id=%s page=%d status_code=%s",
                aweme_id,
                index,
                status_code,
            )
            break

        if not comments:
            if index == 0 and not total_count:
                raise TikTokCommentError("该 TikTok 视频没有评论或评论区已关闭。")
            logger.info(
                "tiktok comment page empty: aweme_id=%s page=%d collected=%d",
                aweme_id,
                index,
                len(collected),
            )
            break

        for item in comments:
            if not isinstance(item, dict):
                continue
            # Pinned comments repeat across pages and would eat the quota.
            cid = str(item.get("cid") or "")
            if cid and cid in seen_cids:
                continue
            if cid:
                seen_cids.add(cid)
            collected.append(normalize_tiktok_comment(item))

        if len(collected) >= max_comments:
            break
        if not payload.get("has_more"):
            break

        next_cursor = payload.get("cursor")
        advanced = int(next_cursor) if isinstance(next_cursor, int) else cursor
        if advanced <= cursor:
            logger.warning(
                "tiktok comment cursor did not advance: aweme_id=%s cursor=%s next=%s",
                aweme_id,
                cursor,
                next_cursor,
            )
            break
        cursor = advanced

        if time.monotonic() > deadline - _DEADLINE_MARGIN_SECONDS:
            logger.warning(
                "tiktok comment fetch hit deadline: aweme_id=%s collected=%d pages=%d",
                aweme_id,
                len(collected),
                index + 1,
            )
            break

        await asyncio.sleep(request_delay)
    else:
        logger.warning(
            "tiktok comment fetch hit page limit: aweme_id=%s pages=%d collected=%d",
            aweme_id,
            max_pages,
            len(collected),
        )

    return collected[:max_comments], total_count


def _status_code_message(status_code: int) -> str:
    if status_code in _STATUS_LOGIN_REQUIRED:
        return "TikTok 评论接口要求登录，请更新 cookies/tiktok.txt。"
    if status_code in _STATUS_ITEM_UNAVAILABLE:
        return "该 TikTok 视频不存在或已被删除。"
    return f"TikTok 评论接口返回错误（status_code={status_code}）。"


class TikTokCommentClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def fetch_comments(
        self, url: str, *, max_comments: int, deadline: float
    ) -> tuple[list[object], int | None]:
        aweme_id = aweme_id_from_url(url)
        if not aweme_id:
            raise TikTokCommentError("无法从该 TikTok 链接解析出视频 ID。")

        budget = deadline - time.monotonic() - _CLOSE_MARGIN_SECONDS
        if budget <= 0:
            raise TikTokCommentError("TikTok 评论抓取超时，请稍后重试。")

        try:
            return await asyncio.wait_for(
                self._fetch_with_browser(url, aweme_id, max_comments, deadline),
                timeout=budget,
            )
        except TimeoutError as e:
            raise TikTokCommentError("TikTok 评论抓取超时，请稍后重试。") from e

    async def _fetch_with_browser(
        self, page_url: str, aweme_id: str, max_comments: int, deadline: float
    ) -> tuple[list[object], int | None]:
        async with self._browser_json_fetcher(page_url) as fetch_json:
            return await paginate_comments(
                fetch_json,
                aweme_id,
                max_comments=max_comments,
                page_size=self._settings.tiktok_comment_page_size,
                max_pages=self._settings.tiktok_comment_max_pages,
                request_delay=self._settings.tiktok_comment_request_delay,
                deadline=deadline,
            )

    @asynccontextmanager
    async def _browser_json_fetcher(self, page_url: str) -> AsyncIterator[JsonFetcher]:
        """Open the video page and yield a fetcher that runs inside it.

        The whole Playwright surface lives here so everything above is testable
        with a plain callable; tests monkeypatch this method.
        """
        try:
            from playwright.async_api import (
                Error as PlaywrightError,
            )
            from playwright.async_api import (
                TimeoutError as PlaywrightTimeoutError,
            )
        except ImportError as exc:
            raise TikTokCommentError(
                "TikTok 评论抓取需要 Playwright 浏览器，当前环境不可用。"
            ) from exc

        timeout_ms = int(self._settings.tiktok_comment_browser_timeout * 1000)
        try:
            async with persistent_context(
                self._settings.tiktok_comment_browser_profile_dir,
                headless=self._settings.tiktok_comment_browser_headless,
                user_agent=_USER_AGENT,
                timeout_ms=timeout_ms,
                viewport=_VIEWPORT,
            ) as context:
                await self._seed_cookies(context)
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(page_url, wait_until="domcontentloaded", timeout=timeout_ms)
                # TikTok keeps navigating after domcontentloaded; evaluating too
                # early dies with "Execution context was destroyed".
                await self._settle_page(page, timeout_ms)
                await self._check_page_state(page)
                await self._wait_for_signer(page)

                async def fetch_json(url: str) -> Any:
                    return await self._browser_request_json(page, url)

                yield fetch_json
        except BrowserUnavailableError as exc:
            logger.error("tiktok comment browser unavailable: %s", exc)
            raise TikTokCommentError(
                "TikTok 评论抓取需要 Playwright 浏览器，当前环境不可用。"
            ) from exc
        except PlaywrightTimeoutError as exc:
            logger.error("tiktok video page goto timed out: url=%s error=%s", page_url, exc)
            raise TikTokCommentError("TikTok 视频页加载超时，请稍后重试。") from exc
        except PlaywrightError as exc:
            logger.error("tiktok comment browser failed: url=%s error=%s", page_url, exc)
            raise TikTokCommentError("TikTok 评论抓取启动浏览器失败，请稍后重试。") from exc

    async def _seed_cookies(self, context: Any) -> None:
        """Load the exported tiktok session into this profile.

        The comment profile is created empty, so without this every request is
        anonymous -- and TikTok answers logged-out clients with an empty body.
        """
        cookie_file = self._settings.cookie_file_for_platform("tiktok")
        cookies = playwright_cookies_from_file(cookie_file, "tiktok.com")
        if not cookies:
            logger.warning(
                "no tiktok cookies to seed (file=%s); comment API will likely return an empty body",
                cookie_file or "<none>",
            )
            return
        await context.add_cookies(cookies)
        logger.info("seeded %d tiktok cookies into comment browser context", len(cookies))

    async def _settle_page(self, page: Any, timeout_ms: int) -> None:
        """Wait out TikTok's post-load navigation before evaluating in the page."""
        try:
            await page.wait_for_load_state("load", timeout=timeout_ms)
        except Exception as exc:
            logger.info("tiktok page load state not reached, continuing: %s", exc)

    async def _check_page_state(self, page: Any) -> None:
        landed = str(page.url)
        marker = _captcha_marker(landed)
        if not marker and await page.query_selector(_CAPTCHA_SELECTOR):
            marker = "captcha-verify-container"
        if marker:
            logger.error("tiktok captcha wall: marker=%s url=%s", marker, landed)
            raise TikTokCommentError(
                "TikTok 触发了人机验证，请稍后重试；如反复出现需在远端执行 "
                "--browser-login tiktok 手工过一次验证。"
            )
        if "/login" in landed:
            logger.warning("tiktok comments require login: landed=%s", landed)
            raise TikTokCommentError("TikTok 评论接口要求登录，请更新 cookies/tiktok.txt。")

    async def _wait_for_signer(self, page: Any) -> None:
        """Give webmssdk.js a chance to hook fetch; proceed anyway on timeout."""
        try:
            await page.wait_for_function(
                _SIGNER_READY_JS, timeout=self._settings.tiktok_comment_signer_wait_ms
            )
        except Exception as exc:
            logger.warning(
                "tiktok signer not ready after %dms, requesting anyway: %s",
                self._settings.tiktok_comment_signer_wait_ms,
                exc,
            )

    async def _browser_request_json(self, page: Any, url: str) -> Any:
        try:
            result = await page.evaluate(_FETCH_JS, {"url": url})
        except Exception as exc:
            if "Execution context was destroyed" not in str(exc):
                raise
            # A late navigation tore down the context mid-evaluate; the page is
            # settled by the time it lands, so one retry is enough.
            logger.info("tiktok page navigated during fetch, retrying once")
            result = await page.evaluate(_FETCH_JS, {"url": url})

        if not isinstance(result, dict):
            raise TikTokCommentError("TikTok 评论接口返回了无法解析的数据。")

        if result.get("error"):
            logger.error("tiktok comment in-page fetch failed: %s", result["error"])
            raise TikTokCommentError("TikTok 评论接口请求失败，请稍后重试。")

        status = int(result.get("status") or 0)
        text = str(result.get("text") or "")
        content_type = str(result.get("contentType") or "")

        if status in (401, 403):
            logger.warning(
                "tiktok comments require login: status=%d cookie_file=%s",
                status,
                bool(self._settings.cookie_file_for_platform("tiktok")),
            )
            raise TikTokCommentError("TikTok 评论接口要求登录，请更新 cookies/tiktok.txt。")
        if status == 429:
            logger.warning("tiktok comments rate limited: status=429")
            raise TikTokCommentError("TikTok 评论接口触发限流，请稍后再试。")

        marker = _captcha_marker(text)
        if marker:
            logger.error("tiktok captcha wall in response: marker=%s", marker)
            raise TikTokCommentError(
                "TikTok 触发了人机验证，请稍后重试；如反复出现需在远端执行 "
                "--browser-login tiktok 手工过一次验证。"
            )

        if not text.strip():
            # TikTok's throttling response for logged-out or distrusted clients.
            logger.warning(
                "tiktok comments empty body: status=%d content_type=%s cookie_present=%s",
                status,
                content_type,
                bool(self._settings.cookie_file_for_platform("tiktok")),
            )
            raise TikTokCommentError(
                "TikTok 评论接口返回空响应，通常表示未登录或被风控；请在远端 Chrome 登录 TikTok。"
            )

        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            logger.error(
                "tiktok comments non-json: status=%d content_type=%s head=%r",
                status,
                content_type,
                text[:200],
            )
            raise TikTokCommentError(
                "TikTok 评论接口返回了非 JSON 响应（可能是验证码或风控页）。"
            ) from exc
