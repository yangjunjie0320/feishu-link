"""TikTok comment fetching by driving the page's own comment panel.

Constructing comment/list requests ourselves does not work, and neither does
reverse engineering the signature. Verified on the production host:

- The video page opens on the "You may like" tab; the comment panel is never
  activated, so the page issues no comment request and the DOM has no comments.
- A hand-built comment/list call from inside the page returns HTTP 200 with an
  empty body and `x-envoy-response-flags: SC` -- the server drops it, because
  the URL lacks the parameters the page signs over (WebIdLastTime, device_id,
  verifyFp and friends).
- Clicking the "Comments" tab makes the page issue its own request, which
  returns real data (53916 bytes, 14 comments, total 4225).

So the browser drives and we listen: activate the panel, scroll it, and collect
the responses TikTok's own code asks for. Nothing here depends on the request
shape, which is also why it should outlive TikTok's parameter changes.

Everything above `_collect_payloads` is plain logic over the captured payloads,
so it tests without a browser.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx

from .browser_session import BrowserUnavailableError, persistent_context
from .config import Settings
from .cookie_utils import playwright_cookies_from_file

logger = logging.getLogger(__name__)

_AWEME_ID_RE = re.compile(r"/(?:video|photo)/(\d+)")
_SHORT_HOSTS = ("vt.tiktok.com", "vm.tiktok.com")

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_VIEWPORT = {"width": 1280, "height": 900}

_COMMENT_API_MARKER = "/api/comment/list"

# TikTok keeps its device identity in Local Storage / IndexedDB, not in cookies.
# A profile seeded with cookies alone still looks like a brand new device and
# gets empty comment bodies; copying this state across is what made it work.
_BROWSER_STATE_DIRS = ("Local Storage", "Session Storage")
_TIKTOK_IDB_PREFIX = "https_www.tiktok.com"

# Distinguishing a captcha wall from a plain login requirement matters: one is
# waited out or cleared by hand, the other needs fresh cookies.
_CAPTCHA_MARKERS = ("captcha", "verify_center", "secsdk-captcha")
_CAPTCHA_SELECTOR = "#captcha-verify-container, .captcha_verify_container"

_STATUS_LOGIN_REQUIRED = frozenset({8, 2154})
_STATUS_ITEM_UNAVAILABLE = frozenset({2053, 10204})

# Leave room inside the caller's deadline for the browser to shut down; a
# cancellation that lands mid-close is exactly how orphan Chromium is created.
_CLOSE_MARGIN_SECONDS = 10.0

# "Comments" also labels a hidden filter in the Activity menu, so match the
# visible leaf node -- the last one in document order is the panel tab.
_TAB_READY_JS = """() => Array.from(document.querySelectorAll("*")).some(
    (e) => e.children.length === 0
        && e.textContent.trim() === "Comments"
        && e.offsetParent !== null
)"""

# Dispatch the full pointer sequence rather than page.mouse.click(): React
# listens on pointerdown, and the tab sits under an overlay that swallows a
# synthetic click at those coordinates.
_CLICK_TAB_JS = """() => {
    const leaves = Array.from(document.querySelectorAll("*")).filter(
        (e) => e.children.length === 0 && e.textContent.trim() === "Comments"
    );
    const visible = leaves.filter((e) => e.offsetParent !== null);
    if (!visible.length) return null;
    const target = visible[visible.length - 1];
    const box = target.getBoundingClientRect();
    const opts = {
        bubbles: true, cancelable: true, view: window,
        clientX: box.x + box.width / 2, clientY: box.y + box.height / 2,
    };
    for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
        const Ctor = type.startsWith("pointer") ? PointerEvent : MouseEvent;
        target.dispatchEvent(new Ctor(type, opts));
    }
    return {y: Math.round(box.y)};
}"""

_COMMENTS_LOADED_JS = """() => document.querySelectorAll(
    '[data-e2e="comment-level-1"]'
).length > 0"""

# Scroll the comment list's own scrollable ancestor; scrolling the window does
# not reach it.
_SCROLL_JS = """() => {
    const items = document.querySelectorAll('[data-e2e="comment-level-1"]');
    if (!items.length) return 0;
    const last = items[items.length - 1];
    last.scrollIntoView({block: "end"});
    let node = last.parentElement;
    while (node) {
        const style = getComputedStyle(node);
        if (/(auto|scroll)/.test(style.overflowY) && node.scrollHeight > node.clientHeight) {
            node.scrollTop = node.scrollHeight;
            break;
        }
        node = node.parentElement;
    }
    return items.length;
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


def comments_from_payloads(
    payloads: Iterable[object], *, max_comments: int
) -> tuple[list[object], int | None]:
    """Merge captured comment/list responses into normalized raw comments.

    Pinned comments repeat across pages, so dedupe by cid. A payload carrying a
    non-zero status_code is reported rather than silently treated as empty.
    """
    collected: list[object] = []
    seen_cids: set[str] = set()
    total_count: int | None = None
    error_status: int | None = None

    for payload in payloads:
        if not isinstance(payload, dict):
            continue

        status_code = payload.get("status_code")
        if isinstance(status_code, int) and status_code != 0:
            error_status = error_status or status_code
            continue

        if total_count is None and isinstance(payload.get("total"), int):
            total_count = payload["total"]

        comments = payload.get("comments")
        if not isinstance(comments, list):
            continue

        for item in comments:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("cid") or "")
            if cid and cid in seen_cids:
                continue
            if cid:
                seen_cids.add(cid)
            collected.append(normalize_tiktok_comment(item))
            if len(collected) >= max_comments:
                return collected, total_count

    if not collected and error_status is not None:
        raise TikTokCommentError(_status_code_message(error_status))

    return collected, total_count


def _status_code_message(status_code: int) -> str:
    if status_code in _STATUS_LOGIN_REQUIRED:
        return "TikTok 评论接口要求登录，请更新 cookies/tiktok.txt。"
    if status_code in _STATUS_ITEM_UNAVAILABLE:
        return "该 TikTok 视频不存在或已被删除。"
    return f"TikTok 评论接口返回错误（status_code={status_code}）。"


def _captcha_marker(text: str) -> str:
    lowered = text.lower()
    for marker in _CAPTCHA_MARKERS:
        if marker in lowered:
            return marker
    return ""


class TikTokCommentClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # How many comment responses came back with an empty body: TikTok's
        # throttling signal, and the difference between "refused" and "never
        # asked" when reporting failure.
        self._empty_responses = 0

    async def fetch_comments(
        self, url: str, *, max_comments: int, deadline: float
    ) -> tuple[list[object], int | None]:
        if not aweme_id_from_url(url):
            raise TikTokCommentError("无法从该 TikTok 链接解析出视频 ID。")

        budget = deadline - time.monotonic() - _CLOSE_MARGIN_SECONDS
        if budget <= 0:
            raise TikTokCommentError("TikTok 评论抓取超时，请稍后重试。")

        try:
            payloads = await asyncio.wait_for(
                self._collect_payloads(url, max_comments=max_comments),
                timeout=budget,
            )
        except TimeoutError as e:
            raise TikTokCommentError("TikTok 评论抓取超时，请稍后重试。") from e

        if not payloads:
            raise TikTokCommentError(self._empty_result_message())

        comments, total = comments_from_payloads(payloads, max_comments=max_comments)
        if not comments:
            raise TikTokCommentError(self._empty_result_message())
        return comments, total

    def _empty_result_message(self) -> str:
        """Separate "TikTok refused" from "the panel never opened".

        An empty body is TikTok's throttling response and is by far the common
        case after repeated fetches, so it must not read as "no comments" --
        that sends the reader looking at the video instead of at the clock.
        """
        if self._empty_responses:
            return "TikTok 返回空评论数据，通常是短时间内请求过多被限流，过一段时间再试即可。"
        return "TikTok 评论面板未加载出评论，请稍后重试。"

    async def _collect_payloads(self, page_url: str, *, max_comments: int) -> list[object]:
        """Drive the page's comment panel and return the responses it received.

        This is the whole Playwright surface; tests monkeypatch this method.
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

        settings = self._settings
        timeout_ms = int(settings.tiktok_comment_browser_timeout * 1000)
        payloads: list[object] = []
        self._empty_responses = 0

        # Must happen before launch: Chromium reads the profile at startup.
        self._seed_browser_state()

        try:
            async with persistent_context(
                settings.tiktok_comment_browser_profile_dir,
                headless=settings.tiktok_comment_browser_headless,
                user_agent=_USER_AGENT,
                timeout_ms=timeout_ms,
                viewport=_VIEWPORT,
            ) as context:
                await self._seed_cookies(context)
                page = context.pages[0] if context.pages else await context.new_page()

                async def on_response(response: Any) -> None:
                    if _COMMENT_API_MARKER not in response.url:
                        return
                    try:
                        text = await response.text()
                    except Exception as exc:
                        logger.info("tiktok comment response unreadable: %s", exc)
                        return
                    if not text.strip():
                        self._empty_responses += 1
                        return
                    try:
                        payloads.append(json.loads(text))
                    except ValueError:
                        marker = _captcha_marker(text)
                        logger.warning(
                            "tiktok comment response not json: captcha_marker=%s head=%r",
                            marker or "none",
                            text[:160],
                        )

                page.on("response", on_response)
                await page.goto(page_url, wait_until="domcontentloaded", timeout=timeout_ms)
                await self._settle_page(page, timeout_ms)
                await self._check_page_state(page)
                await self._activate_comment_panel(page, timeout_ms)
                await self._scroll_for_more(page, payloads, max_comments=max_comments)

                if not payloads and self._empty_responses:
                    logger.warning(
                        "tiktok returned %d empty comment responses: likely throttled "
                        "(cookie_present=%s)",
                        self._empty_responses,
                        bool(settings.cookie_file_for_platform("tiktok")),
                    )
                return payloads
        except BrowserUnavailableError as exc:
            logger.error("tiktok comment browser unavailable: %s", exc)
            raise TikTokCommentError(
                "TikTok 评论抓取需要 Playwright 浏览器，当前环境不可用。"
            ) from exc
        except PlaywrightTimeoutError as exc:
            logger.error("tiktok video page timed out: url=%s error=%s", page_url, exc)
            raise TikTokCommentError("TikTok 视频页加载超时，请稍后重试。") from exc
        except PlaywrightError as exc:
            logger.error("tiktok comment browser failed: url=%s error=%s", page_url, exc)
            raise TikTokCommentError("TikTok 评论抓取启动浏览器失败，请稍后重试。") from exc

    def _seed_browser_state(self) -> None:
        """Copy the device identity TikTok keeps outside cookies.

        Local Storage and IndexedDB carry what TikTok uses to recognize a
        browser; a profile holding only cookies still reads as a new device and
        gets empty comment bodies. Verified: the same video failed with cookies
        alone and returned 70679 bytes once this state was copied across.

        Runs once per profile -- the copy is ~20MB and the state does not need
        to stay in sync afterwards. Best effort: any failure is logged and the
        fetch proceeds.
        """
        source = Path(self._settings.tiktok_comment_chrome_profile_dir).expanduser()
        if not self._settings.tiktok_comment_seed_browser_state or not source.is_dir():
            return

        target = Path(self._settings.tiktok_comment_browser_profile_dir) / "Default"
        if (target / "Local Storage").exists():
            return

        target.mkdir(parents=True, exist_ok=True)
        for name in _BROWSER_STATE_DIRS:
            src = source / name
            if not src.is_dir():
                continue
            try:
                shutil.copytree(src, target / name, dirs_exist_ok=True)
            except OSError as exc:
                logger.warning("failed to copy %s into tiktok profile: %s", name, exc)

        # Only TikTok's own IndexedDB is needed; the rest of the store is large
        # and irrelevant.
        idb_src = source / "IndexedDB"
        if idb_src.is_dir():
            idb_dst = target / "IndexedDB"
            idb_dst.mkdir(parents=True, exist_ok=True)
            for entry in idb_src.iterdir():
                if not entry.name.startswith(_TIKTOK_IDB_PREFIX):
                    continue
                try:
                    shutil.copytree(entry, idb_dst / entry.name, dirs_exist_ok=True)
                except OSError as exc:
                    logger.warning("failed to copy %s into tiktok profile: %s", entry.name, exc)
        logger.info("seeded browser state into tiktok comment profile from %s", source)

    async def _seed_cookies(self, context: Any) -> None:
        """Load the exported tiktok session into this profile.

        The comment profile is created empty, so without this every request is
        anonymous -- and TikTok answers logged-out clients with an empty body.
        """
        cookie_file = self._settings.cookie_file_for_platform("tiktok")
        cookies = playwright_cookies_from_file(cookie_file, "tiktok.com")
        if not cookies:
            logger.warning(
                "no tiktok cookies to seed (file=%s); comments will likely be empty",
                cookie_file or "<none>",
            )
            return
        await context.add_cookies(cookies)
        logger.info("seeded %d tiktok cookies into comment browser context", len(cookies))

    async def _settle_page(self, page: Any, timeout_ms: int) -> None:
        """Wait out TikTok's post-load navigation before touching the page."""
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
                "TikTok 触发了人机验证，请稍后重试；如反复出现需在远端 Chrome 手工过一次验证。"
            )
        if "/login" in landed:
            logger.warning("tiktok comments require login: landed=%s", landed)
            raise TikTokCommentError("TikTok 评论接口要求登录，请更新 cookies/tiktok.txt。")

    async def _activate_comment_panel(self, page: Any, timeout_ms: int) -> None:
        """Click the Comments tab; the page requests nothing until it is open.

        The tab renders before React binds its handler, so a click can report
        success and do nothing -- hence retrying until comments actually appear.
        """
        try:
            await page.wait_for_function(_TAB_READY_JS, timeout=timeout_ms)
        except Exception as exc:
            logger.warning("tiktok comments tab never rendered: %s", exc)
            return

        settle = self._settings.tiktok_comment_tab_settle_seconds
        attempts = max(1, self._settings.tiktok_comment_tab_click_attempts)
        for attempt in range(attempts):
            await asyncio.sleep(settle)
            spot = await page.evaluate(_CLICK_TAB_JS)
            if not isinstance(spot, dict):
                logger.warning("tiktok comments tab not clickable")
                return
            try:
                await page.wait_for_function(
                    _COMMENTS_LOADED_JS,
                    timeout=int(self._settings.tiktok_comment_load_timeout * 1000),
                )
                logger.info("tiktok comment panel activated on attempt %d", attempt + 1)
                return
            except Exception:
                logger.info("tiktok comments not loaded after click attempt %d", attempt + 1)
        logger.warning("tiktok comment panel never loaded comments")

    async def _scroll_for_more(
        self, page: Any, payloads: list[object], *, max_comments: int
    ) -> None:
        """Scroll the panel so TikTok requests further pages itself."""
        collected = _count_comments(payloads)
        stale_rounds = 0
        for index in range(self._settings.tiktok_comment_max_scrolls):
            if collected >= max_comments:
                return
            try:
                await page.evaluate(_SCROLL_JS)
            except Exception as exc:
                logger.info("tiktok comment scroll failed at round %d: %s", index, exc)
                return
            await asyncio.sleep(self._settings.tiktok_comment_scroll_delay)

            grown = _count_comments(payloads)
            if grown == collected:
                stale_rounds += 1
                if stale_rounds >= 2:
                    logger.info(
                        "tiktok comments stopped growing at %d after %d scrolls",
                        collected,
                        index + 1,
                    )
                    return
            else:
                stale_rounds = 0
            collected = grown


def _count_comments(payloads: Iterable[object]) -> int:
    total = 0
    for payload in payloads:
        if isinstance(payload, dict) and isinstance(payload.get("comments"), list):
            total += len(payload["comments"])
    return total
