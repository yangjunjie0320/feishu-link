from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .bibi_models import SummaryResult
from .config import Settings
from .cookie_utils import get_cookie_header

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
_OUTPUT_INSTRUCTIONS = """\

Output requirements:
- Do not use any emoji.
- Use Markdown formatting.
- Do not use Markdown headings or standalone section titles.
- Use nested bullet points only to separate sections and show hierarchy.
- Use "-" as the only unordered bullet marker; do not use "*" or "+" for lists.
- Use four spaces for each nested bullet level.
- Do not use numbered lists."""


class BibiAPIError(Exception):
    """Raised when the BibiGPT API returns an error."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"BibiGPT API error (HTTP {status_code}): {_summarize_error_body(body)}")


class AuthenticationError(BibiAPIError):
    """Raised when cookie authentication fails (401/403)."""


@dataclass(frozen=True)
class _BibiRoutes:
    api_base_url: str
    referer: str
    origin: str
    cookie_domain: str


class BibiClient:
    """BibiGPT web API client using cookie-based authentication."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._routes = _resolve_routes(settings.bibigpt_base_url)
        self._cookie_file = settings.cookie_file_for_platform("bibigpt")
        cookie_header = get_cookie_header(
            self._cookie_file,
            self._routes.cookie_domain,
        )
        self._cookie_error = _validate_supabase_auth_cookie(cookie_header)
        self._browser_lock = asyncio.Lock()

        self._headers = {
            "User-Agent": _USER_AGENT,
            "Referer": self._routes.referer,
            "Origin": self._routes.origin,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if cookie_header and not self._cookie_error:
            self._headers["Cookie"] = cookie_header
        elif self._cookie_error:
            logger.warning("BibiGPT cookie ignored: %s", self._cookie_error)
        logger.info(
            "BibiClient initialized (api_base_url=%s, referer=%s)",
            self._routes.api_base_url,
            self._routes.referer,
        )

    async def summarize(
        self,
        video_url: str,
        prompt: str | None = None,
    ) -> SummaryResult:
        """Summarize a video using the chat/completions endpoint.

        Args:
            video_url: URL of the video to summarize.
            prompt: Custom prompt. Falls back to settings.default_prompt.

        Returns:
            SummaryResult with the AI-generated summary.
        """
        if self._cookie_error and self._settings.bibigpt_access_mode != "browser":
            raise AuthenticationError(0, self._cookie_error)

        effective_prompt = _with_output_instructions(
            prompt or self._settings.bibigpt_default_prompt
        )

        if self._settings.bibigpt_access_mode == "api":
            return await self._summarize_api(video_url, effective_prompt)
        if self._settings.bibigpt_access_mode == "browser":
            return await self._summarize_browser(video_url, effective_prompt)
        return await self._summarize_web(video_url, effective_prompt)

    async def _summarize_api(self, video_url: str, prompt: str) -> SummaryResult:
        body: dict[str, Any] = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "video_url", "video_url": {"url": video_url}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            "stream": False,
        }

        url = f"{self._routes.api_base_url}/api/v1/chat/completions"
        logger.info("Requesting summary for %s", video_url)

        async with httpx.AsyncClient(timeout=self._settings.bibigpt_timeout) as client:
            response = await client.post(url, json=body, headers=self._headers)

        self._check_response(response)
        data = response.json()

        result = SummaryResult.from_response(data, video_url=video_url)
        logger.info(
            "Summary complete (model=%s, tokens=%d, cached=%s)",
            result.model,
            result.usage.total_tokens,
            result.from_cache,
        )
        return result

    async def _summarize_browser(self, video_url: str, prompt: str) -> SummaryResult:
        body: dict[str, Any] = {
            "0": {
                "json": {
                    "url": video_url,
                    "promptConfig": _web_prompt_config(prompt),
                }
            }
        }

        url = f"{self._routes.api_base_url}/api/trpc/video.summaryBySetting?batch=1"
        logger.info("Requesting browser-backed web summary for %s", video_url)

        raw_data = await self._browser_fetch_json(url, body)
        data = _extract_trpc_data(raw_data)

        result = SummaryResult.from_web_response(data, video_url=video_url)
        logger.info(
            "Browser-backed web summary complete (model=%s, cached=%s)",
            result.model,
            result.from_cache,
        )
        return result

    async def _summarize_web(self, video_url: str, prompt: str) -> SummaryResult:
        body: dict[str, Any] = {
            "0": {
                "json": {
                    "url": video_url,
                    "promptConfig": _web_prompt_config(prompt),
                }
            }
        }

        url = f"{self._routes.api_base_url}/api/trpc/video.summaryBySetting?batch=1"
        logger.info("Requesting web summary for %s", video_url)

        async with httpx.AsyncClient(timeout=self._settings.bibigpt_timeout) as client:
            response = await client.post(url, json=body, headers=self._headers)

        self._check_response(response)
        data = _extract_trpc_data(response.json())

        result = SummaryResult.from_web_response(data, video_url=video_url)
        logger.info(
            "Web summary complete (model=%s, cached=%s)",
            result.model,
            result.from_cache,
        )
        return result

    async def get_user_info(self) -> dict[str, Any]:
        """Fetch current user profile to verify cookie validity."""
        if self._settings.bibigpt_access_mode == "browser":
            raw_data = await self._browser_fetch_json(
                f"{self._routes.api_base_url}/api/trpc/user.me",
                None,
                method="GET",
            )
            data = _extract_user_data(raw_data)
            logger.info(
                "Authenticated via browser profile as %s (%s)",
                data.get("user_metadata", {}).get("full_name", "unknown"),
                data.get("email", "unknown"),
            )
            return data

        if self._cookie_error:
            raise AuthenticationError(0, self._cookie_error)

        url = f"{self._routes.api_base_url}/api/trpc/user.me"
        logger.debug("Verifying cookie via user.me")

        async with httpx.AsyncClient(timeout=self._settings.request_timeout) as client:
            response = await client.get(url, headers=self._headers)

        self._check_response(response)
        data: dict[str, Any] = response.json()

        # tRPC wraps in result.data.json
        user = data.get("result", {}).get("data", {}).get("json", {})
        logger.info(
            "Authenticated as %s (%s)",
            user.get("user_metadata", {}).get("full_name", "unknown"),
            user.get("email", "unknown"),
        )
        return user

    def _check_response(self, response: httpx.Response) -> None:
        if response.status_code in (401, 403):
            raise AuthenticationError(response.status_code, response.text)
        if not response.is_success:
            raise BibiAPIError(response.status_code, response.text)

    async def _browser_fetch_json(
        self,
        url: str,
        body: dict[str, Any] | None,
        *,
        method: str = "POST",
    ) -> Any:
        try:
            from playwright.async_api import (
                Error as PlaywrightError,
            )
            from playwright.async_api import (
                TimeoutError as PlaywrightTimeoutError,
            )
            from playwright.async_api import (
                async_playwright,
            )
        except ImportError as exc:
            raise BibiAPIError(
                0,
                "Playwright is not installed; rebuild the Docker image or run uv sync.",
            ) from exc

        profile_dir = Path(self._settings.bibigpt_browser_profile_dir)
        profile_dir.mkdir(parents=True, exist_ok=True)
        timeout_ms = int(self._settings.bibigpt_browser_timeout * 1000)

        async with self._browser_lock:
            try:
                async with async_playwright() as playwright:
                    context = await playwright.chromium.launch_persistent_context(
                        user_data_dir=str(profile_dir),
                        headless=self._settings.bibigpt_browser_headless,
                        user_agent=_USER_AGENT,
                        viewport={"width": 1280, "height": 900},
                        args=[
                            "--disable-dev-shm-usage",
                            "--disable-gpu",
                            "--no-sandbox",
                        ],
                        timeout=timeout_ms,
                    )
                    try:
                        await self._seed_browser_cookies(context)
                        page = context.pages[0] if context.pages else await context.new_page()
                        await page.goto(
                            self._routes.referer,
                            wait_until="domcontentloaded",
                            timeout=timeout_ms,
                        )
                        result = await page.evaluate(
                            """async ({url, method, body}) => {
                                const init = {
                                    method,
                                    credentials: "include",
                                    headers: {
                                        "accept": "application/json",
                                        "content-type": "application/json"
                                    }
                                };
                                if (body !== null) {
                                    init.body = JSON.stringify(body);
                                }
                                const response = await fetch(url, init);
                                return {
                                    status: response.status,
                                    ok: response.ok,
                                    text: await response.text()
                                };
                            }""",
                            {"url": url, "method": method, "body": body},
                        )
                    finally:
                        await context.close()
            except PlaywrightTimeoutError as exc:
                raise BibiAPIError(
                    0,
                    f"BibiGPT browser request timed out after "
                    f"{self._settings.bibigpt_browser_timeout:g} seconds.",
                ) from exc
            except PlaywrightError as exc:
                raise BibiAPIError(0, f"BibiGPT browser request failed: {exc}") from exc

        if not isinstance(result, dict):
            raise BibiAPIError(0, "BibiGPT browser request returned an unexpected result.")

        status = int(result.get("status") or 0)
        text = str(result.get("text") or "")
        if status in (401, 403):
            raise AuthenticationError(status, text)
        if status < 200 or status >= 300:
            raise BibiAPIError(status, text)

        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise BibiAPIError(status, text) from exc

    async def _seed_browser_cookies(self, context: Any) -> None:
        if not self._cookie_file or self._cookie_error:
            if self._cookie_error:
                logger.warning(
                    "Skipping BibiGPT cookie import for browser mode: %s",
                    self._cookie_error,
                )
            return

        cookies = _playwright_cookies_from_file(
            self._cookie_file,
            self._routes.cookie_domain,
        )
        if not cookies:
            return

        await context.add_cookies(cookies)
        logger.debug("Seeded %d BibiGPT cookies into browser context", len(cookies))


def _with_output_instructions(prompt: str) -> str:
    return f"{prompt.strip()}\n\n{_OUTPUT_INSTRUCTIONS.strip()}"


def _web_prompt_config(prompt: str) -> dict[str, Any]:
    return {
        "model": "",
        "customPrompt": prompt,
        "customPromptTitle": "Feishu Link",
        "outputLanguage": "中文",
        "audioLanguage": "auto",
        "whisperPrompt": "",
        "autoTranslateLanguage": "",
        "transcribeProvider": "auto",
        "showEmoji": False,
        "detailLevel": 1500,
        "sentenceNumber": 5,
        "isRefresh": False,
        "showTimestamp": True,
    }


def _extract_trpc_data(data: Any) -> dict[str, Any]:
    item = data[0] if isinstance(data, list) and data else data
    if not isinstance(item, dict):
        raise ValueError("BibiGPT web response has unexpected shape")

    if "error" in item:
        raise BibiAPIError(200, json.dumps(item["error"], ensure_ascii=False))

    result = item.get("result", item)
    if not isinstance(result, dict):
        raise ValueError("BibiGPT web response result has unexpected shape")

    payload = result.get("data", result)
    if isinstance(payload, dict) and "json" in payload:
        payload = payload["json"]
    if not isinstance(payload, dict):
        raise ValueError("BibiGPT web response data has unexpected shape")
    return payload


def _extract_user_data(data: Any) -> dict[str, Any]:
    item = data[0] if isinstance(data, list) and data else data
    if not isinstance(item, dict):
        raise ValueError("BibiGPT user response has unexpected shape")

    result = item.get("result", item)
    payload = result.get("data", result) if isinstance(result, dict) else None
    if isinstance(payload, dict) and "json" in payload:
        payload = payload["json"]

    if payload is None:
        raise AuthenticationError(
            401,
            "BibiGPT browser profile is not logged in; log in or provide complete cookies.",
        )
    if not isinstance(payload, dict):
        raise ValueError("BibiGPT user response data has unexpected shape")
    return payload


def _playwright_cookies_from_file(cookie_file: str, domain: str) -> list[dict[str, Any]]:
    path = Path(cookie_file)
    if not path.exists():
        return []

    jar = MozillaCookieJar(str(path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception as exc:
        logger.warning("Failed to parse BibiGPT cookies for browser mode: %s", exc)
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


def _summarize_error_body(body: str) -> str:
    stripped = body.strip()
    if not stripped:
        return "empty response body"
    if stripped.startswith("<!DOCTYPE html") or stripped.startswith("<html"):
        return "service returned an HTML error page"
    return stripped[:200]


def _validate_supabase_auth_cookie(cookie_header: str) -> str:
    if not cookie_header:
        return ""

    chunks: list[tuple[int, str]] = []
    for part in cookie_header.split("; "):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        prefix, dot, suffix = name.rpartition(".")
        if not dot or not prefix.endswith("-auth-token"):
            continue
        try:
            index = int(suffix)
        except ValueError:
            continue
        chunks.append((index, value))

    if not chunks:
        return ""

    chunks.sort(key=lambda item: item[0])
    if [index for index, _ in chunks] != list(range(len(chunks))):
        return "BibiGPT auth cookie chunks are incomplete; please re-export aitodo.co cookies."

    value = "".join(chunk for _, chunk in chunks)
    if not value.startswith("base64-"):
        return ""

    encoded = value.removeprefix("base64-")
    try:
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        decoded.decode("utf-8")
    except Exception:
        return "BibiGPT auth cookie is corrupted or incomplete; please re-export aitodo.co cookies."

    return ""


def _resolve_routes(base_url: str) -> _BibiRoutes:
    normalized = (base_url or "https://bibigpt.co").strip().rstrip("/")
    if "://" not in normalized:
        normalized = f"https://{normalized}"

    parsed = urlsplit(normalized)
    origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    referer = normalized if parsed.path and parsed.path != "/" else origin
    cookie_domain = parsed.hostname or parsed.netloc.split(":")[0]

    return _BibiRoutes(
        api_base_url=origin,
        referer=f"{referer}/",
        origin=origin,
        cookie_domain=cookie_domain,
    )
