from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .bibi_models import (
    ChapterSummaryFetchResult,
    SummaryResult,
    parse_chapter_summary_response,
)
from .browser_session import BrowserUnavailableError, persistent_context
from .config import Settings
from .cookie_refresh import write_netscape
from .cookie_utils import get_cookie_header, playwright_cookies_from_file

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
_CONTENT_ID_RECOVERY_DELAYS: tuple[float, ...] = (3.0, 8.0)
_OUTPUT_INSTRUCTIONS = """\

输出要求:
- 必须用简体中文输出, 非中文内容要翻译成中文。不要使用 emoji。
- 使用 Markdown，尽量保留原有结构。
- Markdown 各级标题都改成无序列表+加粗。
- 允许使用多级无序列表，用缩进表达层级。
- 无序列表只能使用 "-", 不要使用 "*" 或 "+"。
- 不要使用编号列表。"""


class BibiAPIError(Exception):
    """Raised when the BibiGPT API returns an error."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"BibiGPT API error (HTTP {status_code}): {_summarize_error_body(body)}")


class AuthenticationError(BibiAPIError):
    """Raised when cookie authentication fails (401/403)."""


class TranscriptUnavailableError(BibiAPIError):
    """Raised when BibiGPT asks for a transcript instead of returning a summary."""


@dataclass(frozen=True)
class _BibiRoutes:
    api_base_url: str
    referer: str
    browser_page_url: str
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
        """Summarize a video using the BibiGPT web endpoint.

        Args:
            video_url: URL of the video to summarize.
            prompt: Custom prompt. Falls back to settings.default_prompt.

        Returns:
            SummaryResult with the AI-generated summary.
        """
        if self._cookie_error and self._settings.bibigpt_access_mode != "browser":
            raise AuthenticationError(0, self._cookie_error)

        base_prompt = (prompt or self._settings.bibigpt_default_prompt).strip()
        effective_prompt = _with_output_instructions(base_prompt) if base_prompt else ""

        if self._settings.bibigpt_access_mode == "browser":
            result = await self._summarize_browser(video_url, effective_prompt)
            if not result.content_id:
                result = await self._recover_content_id(result, effective_prompt)
            return result
        return await self._summarize_web(video_url, effective_prompt)

    async def summarize_cached(
        self,
        video_url: str,
        prompt: str | None = None,
    ) -> SummaryResult | None:
        """Fetch the already-generated summary for a video: isRefresh=false
        returns BibiGPT's stored record without regenerating (no quota, no
        1-2 minute wait). Returns None when the lookup fails or yields no
        usable summary so callers can fall back to a full summarize()."""
        if self._cookie_error and self._settings.bibigpt_access_mode != "browser":
            return None
        base_prompt = (prompt or self._settings.bibigpt_default_prompt).strip()
        effective_prompt = _with_output_instructions(base_prompt) if base_prompt else ""
        try:
            if self._settings.bibigpt_access_mode == "browser":
                return await self._summarize_browser(video_url, effective_prompt, refresh=False)
            return await self._summarize_web(video_url, effective_prompt)
        except Exception as e:
            logger.warning(
                "BibiGPT cached summary lookup failed, falling back to regeneration: %s", e
            )
            return None

    async def _recover_content_id(
        self,
        result: SummaryResult,
        prompt: str,
    ) -> SummaryResult:
        """补取 contentId：isRefresh=true 首次生成的响应常缺 contentId（记录异步落库），
        用相同 promptConfig、isRefresh=false 重查已落库记录，不触发重新生成。"""
        for delay in _CONTENT_ID_RECOVERY_DELAYS:
            await asyncio.sleep(delay)
            try:
                lookup = await self._summarize_browser(
                    result.video_url,
                    prompt,
                    refresh=False,
                )
            except Exception as exc:
                logger.warning("BibiGPT contentId recovery lookup failed: %s", exc)
                continue
            if lookup.content_id:
                logger.info(
                    "BibiGPT contentId recovered via cache lookup (content_id=%s)",
                    lookup.content_id,
                )
                return replace(result, content_id=lookup.content_id)
        logger.warning(
            "BibiGPT contentId recovery exhausted retries for %s",
            _source_url_for_log(result.video_url),
        )
        return result

    async def fetch_chapter_summary(
        self,
        summary: SummaryResult,
    ) -> ChapterSummaryFetchResult:
        """Fetch BibiGPT's Chinese timeline summary for an existing content item."""
        content_id = summary.content_id.strip()
        if not content_id:
            logger.warning("BibiGPT chapter summary unavailable because content_id is missing")
            return ChapterSummaryFetchResult(
                introduction="",
                sections=(),
                status="unavailable",
                source="none",
                reason="BibiGPT summary response did not include contentId.",
            )

        try:
            if self._settings.bibigpt_access_mode == "browser":
                data = await self._fetch_chapter_summary_browser(content_id)
            else:
                if self._cookie_error:
                    raise AuthenticationError(0, self._cookie_error)
                data = await self._fetch_chapter_summary_web(content_id)
        except Exception as exc:
            reason = _chapter_summary_fetch_error_reason(exc)
            logger.warning(
                "BibiGPT chapter summary lookup failed (content_id=%s, reason=%s)",
                content_id,
                reason,
            )
            return ChapterSummaryFetchResult(
                introduction="",
                sections=(),
                status="error",
                source="none",
                reason=reason,
            )

        if data is None:
            result = ChapterSummaryFetchResult(
                introduction="",
                sections=(),
                status="unavailable",
                source="none",
                reason="BibiGPT has no timeline chapter summary for this content.",
            )
        else:
            introduction, sections = parse_chapter_summary_response(data)
            if sections:
                result = ChapterSummaryFetchResult(
                    introduction=introduction,
                    sections=sections,
                    status="available",
                    source="video.chapterSummary",
                )
            else:
                result = ChapterSummaryFetchResult(
                    introduction=introduction,
                    sections=(),
                    status="unavailable",
                    source="none",
                    reason="BibiGPT has no timeline chapter summary for this content.",
                )

        logger.info(
            "BibiGPT chapter summary lookup complete "
            "(content_id=%s, status=%s, source=%s, sections=%d)",
            content_id,
            result.status,
            result.source,
            len(result.sections),
        )
        return result

    async def _fetch_chapter_summary_web(
        self,
        content_id: str,
    ) -> dict[str, Any] | None:
        payload = _chapter_summary_payload(content_id)
        url = f"{self._routes.api_base_url}/api/trpc/video.chapterSummary"
        async with httpx.AsyncClient(timeout=self._settings.bibigpt_timeout) as client:
            response = await client.get(
                url,
                params={"input": _encode_trpc_input(payload)},
                headers=self._headers,
            )
        self._check_response(response)
        return _extract_optional_trpc_data(response.json())

    async def _fetch_chapter_summary_browser(
        self,
        content_id: str,
    ) -> dict[str, Any] | None:
        url = _trpc_query_url(
            self._routes.api_base_url,
            "video.chapterSummary",
            _chapter_summary_payload(content_id),
        )
        raw_data = await self._browser_fetch_json(url, None, method="GET")
        return _extract_optional_trpc_data(raw_data)

    async def _summarize_browser(
        self,
        video_url: str,
        prompt: str,
        *,
        refresh: bool = True,
    ) -> SummaryResult:
        body: dict[str, Any] = {
            "0": {
                "json": {
                    "url": video_url,
                    "promptConfig": _web_prompt_config(
                        prompt,
                        model=self._settings.bibigpt_model,
                        refresh=refresh,
                    ),
                }
            }
        }

        url = f"{self._routes.api_base_url}/api/trpc/video.summaryBySetting?batch=1"
        logger.info(
            "Requesting browser-backed web summary for %s",
            _source_url_for_log(video_url),
        )

        raw_data = await self._browser_fetch_json(url, body)
        data = _extract_trpc_data(raw_data)

        result = _validate_summary_result(
            SummaryResult.from_web_response(data, video_url=video_url)
        )
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
                    "promptConfig": _web_prompt_config(
                        prompt,
                        model=self._settings.bibigpt_model,
                    ),
                }
            }
        }

        url = f"{self._routes.api_base_url}/api/trpc/video.summaryBySetting?batch=1"
        logger.info("Requesting web summary for %s", _source_url_for_log(video_url))

        async with httpx.AsyncClient(timeout=self._settings.bibigpt_timeout) as client:
            response = await client.post(url, json=body, headers=self._headers)

        self._check_response(response)
        data = _extract_trpc_data(response.json())

        result = _validate_summary_result(
            SummaryResult.from_web_response(data, video_url=video_url)
        )
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
        async with self._browser_page() as page:
            return await self._browser_request_json(page, url, body, method=method)

    @asynccontextmanager
    async def _browser_page(self) -> AsyncIterator[Any]:
        try:
            from playwright.async_api import (
                Error as PlaywrightError,
            )
            from playwright.async_api import (
                TimeoutError as PlaywrightTimeoutError,
            )
        except ImportError as exc:
            raise BibiAPIError(
                0,
                "Playwright is not installed; rebuild the Docker image or run uv sync.",
            ) from exc

        timeout_ms = int(self._settings.bibigpt_browser_timeout * 1000)

        try:
            async with persistent_context(
                self._settings.bibigpt_browser_profile_dir,
                headless=self._settings.bibigpt_browser_headless,
                user_agent=_USER_AGENT,
                timeout_ms=timeout_ms,
                viewport={"width": 1280, "height": 900},
            ) as context:
                await self._seed_browser_cookies(context)
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(
                    self._routes.browser_page_url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                yield page
                if self._settings.bibigpt_cookie_writeback:
                    await self._writeback_cookies(context)
        except BrowserUnavailableError as exc:
            raise BibiAPIError(0, str(exc)) from exc
        except PlaywrightTimeoutError as exc:
            raise BibiAPIError(
                0,
                f"BibiGPT browser request timed out after "
                f"{self._settings.bibigpt_browser_timeout:g} seconds.",
            ) from exc
        except PlaywrightError as exc:
            raise BibiAPIError(0, f"BibiGPT browser request failed: {exc}") from exc

    async def _browser_request_json(
        self,
        page: Any,
        url: str,
        body: dict[str, Any] | None,
        *,
        method: str = "POST",
    ) -> Any:
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

        cookies = playwright_cookies_from_file(
            self._cookie_file,
            self._routes.cookie_domain,
        )
        if not cookies:
            return

        await context.add_cookies(cookies)
        logger.debug("Seeded %d BibiGPT cookies into browser context", len(cookies))

    async def _writeback_cookies(self, context: Any) -> None:
        """Persist the live context's cookies so the HTTP path stays fresh."""
        if not self._cookie_file:
            return
        try:
            raw = await context.cookies()
        except Exception as exc:
            logger.warning("Failed to read BibiGPT cookies for writeback: %s", exc)
            return

        domain = self._routes.cookie_domain
        cookies = [
            cookie
            for cookie in raw
            if _domain_matches_writeback(str(cookie.get("domain", "")), domain)
        ]
        if not cookies:
            return

        write_netscape(cookies, self._cookie_file)
        logger.debug("Wrote back %d BibiGPT cookies to %s", len(cookies), self._cookie_file)


def _domain_matches_writeback(cookie_domain: str, want: str) -> bool:
    d = cookie_domain.lstrip(".")
    return d == want or want.endswith(f".{d}")


def _source_url_for_log(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _with_output_instructions(prompt: str) -> str:
    return f"{prompt.strip()}\n\n{_OUTPUT_INSTRUCTIONS.strip()}"


def _encode_trpc_input(payload: dict[str, Any]) -> str:
    return json.dumps({"json": payload}, ensure_ascii=False, separators=(",", ":"))


def _trpc_query_url(base_url: str, path: str, payload: dict[str, Any]) -> str:
    url = f"{base_url}/api/trpc/{path}"
    return str(httpx.URL(url, params={"input": _encode_trpc_input(payload)}))


def _chapter_summary_payload(content_id: str) -> dict[str, Any]:
    return {
        "contentId": content_id,
        "outputLanguage": "中文",
        "summaryType": "timeline",
    }


def _chapter_summary_fetch_error_reason(exc: Exception) -> str:
    if isinstance(exc, AuthenticationError):
        return "BibiGPT authentication failed."
    if isinstance(exc, httpx.TimeoutException):
        return "BibiGPT chapter summary request timed out."
    if isinstance(exc, BibiAPIError):
        return _chapter_summary_api_error_reason(exc)
    if isinstance(exc, (json.JSONDecodeError, ValueError, TypeError)):
        return "BibiGPT returned an unexpected chapter summary response."
    if isinstance(exc, httpx.HTTPError):
        return "BibiGPT chapter summary request failed."
    return f"BibiGPT chapter summary lookup failed ({type(exc).__name__})."


def _chapter_summary_api_error_reason(exc: BibiAPIError) -> str:
    if exc.status_code == 402:
        return "BibiGPT chapter summary quota is exhausted or paid access is required."
    if exc.status_code == 429:
        return "BibiGPT chapter summary request was rate limited."
    if exc.status_code >= 500:
        return "BibiGPT chapter summary service returned a server error."
    if exc.status_code != 200:
        return f"BibiGPT API returned HTTP {exc.status_code}."

    code, http_status, message = _trpc_error_metadata(exc.body)
    normalized_code = code.upper()
    normalized_message = message.lower()
    if http_status == 401 or normalized_code == "UNAUTHORIZED":
        return "BibiGPT authentication failed."
    if (
        http_status == 402
        or normalized_code == "PAYMENT_REQUIRED"
        or "payment required" in normalized_message
        or "余额不足" in message
    ):
        return "BibiGPT chapter summary quota is exhausted or paid access is required."
    if http_status == 403 or normalized_code == "FORBIDDEN":
        return "BibiGPT account does not have access to chapter summaries."
    if http_status == 429 or normalized_code == "TOO_MANY_REQUESTS":
        return "BibiGPT chapter summary request was rate limited."
    if http_status is not None and http_status >= 500:
        return "BibiGPT chapter summary service returned a server error."

    safe_code = re.sub(r"[^A-Za-z0-9_.-]", "", code)[:40]
    details: list[str] = []
    if safe_code:
        details.append(f"code={safe_code}")
    if http_status is not None:
        details.append(f"status={http_status}")
    suffix = f" ({', '.join(details)})" if details else ""
    return f"BibiGPT chapter summary API returned an error{suffix}."


def _trpc_error_metadata(body: str) -> tuple[str, int | None, str]:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return "", None, ""

    code = ""
    http_status: int | None = None
    message = ""
    pending: list[Any] = [payload]
    while pending:
        candidate = pending.pop(0)
        if isinstance(candidate, list):
            pending.extend(candidate)
            continue
        if not isinstance(candidate, dict):
            continue
        candidate_message = candidate.get("message")
        if not message and isinstance(candidate_message, str):
            message = candidate_message[:200]
        candidate_status = candidate.get("httpStatus")
        if http_status is None and type(candidate_status) is int:
            http_status = candidate_status
        candidate_code = candidate.get("code")
        if not code and isinstance(candidate_code, str):
            code = candidate_code[:80]
        pending.extend(value for value in candidate.values() if isinstance(value, (dict, list)))
    return code, http_status, message


def _validate_summary_result(result: SummaryResult) -> SummaryResult:
    if _looks_like_missing_transcript_response(result.content):
        raise TranscriptUnavailableError(
            200,
            (
                "BibiGPT did not receive a transcript for this video. "
                "The video may have no captions, blocked captions, or a temporary "
                "YouTube transcript fetch issue."
            ),
        )
    return result


def _looks_like_missing_transcript_response(content: str) -> bool:
    normalized = " ".join(content.strip().lower().split())
    if not normalized:
        return False
    return any(
        phrase in normalized
        for phrase in (
            "please provide the transcript",
            "paste the text",
            "once you paste",
            "请提供",
            "转录文本",
            "粘贴文本",
        )
    )


def _web_prompt_config(prompt: str, *, model: str = "", refresh: bool = False) -> dict[str, Any]:
    return {
        "model": model,
        "customPrompt": prompt,
        "customPromptTitle": "Feishu Link",
        "outputLanguage": "中文",
        "audioLanguage": "auto",
        "whisperPrompt": "",
        "autoTranslateLanguage": "中文",
        "transcribeProvider": "auto",
        "showEmoji": False,
        "detailLevel": 1500,
        "sentenceNumber": 5,
        "isRefresh": refresh,
        "showTimestamp": True,
    }


def _extract_trpc_data(data: Any) -> dict[str, Any]:
    payload = _extract_trpc_payload(data)
    if not isinstance(payload, dict):
        raise ValueError("BibiGPT web response data has unexpected shape")
    return payload


def _extract_optional_trpc_data(data: Any) -> dict[str, Any] | None:
    payload = _extract_trpc_payload(data)
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("BibiGPT web response data has unexpected shape")
    return payload


def _extract_trpc_payload(data: Any) -> Any:
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


def share_page_url(base_url: str, video_url: str, content_id: str = "") -> str:
    """The shareable BibiGPT summary page for a video. With a contentId the
    site's own share page is {origin}/content/{contentId} (the ?r= referral
    suffix is optional); the API sometimes omits contentId even after
    recovery, so the open-by-prefix form {base}/{video_url} — which is always
    constructible and shows the same summary — is the fallback."""
    routes = _resolve_routes(base_url)
    if content_id:
        return f"{routes.origin}/content/{content_id}"
    return routes.referer + video_url


def _resolve_routes(base_url: str) -> _BibiRoutes:
    normalized = (base_url or "https://bibigpt.co").strip().rstrip("/")
    if "://" not in normalized:
        normalized = f"https://{normalized}"

    parsed = urlsplit(normalized)
    origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    path = parsed.path.rstrip("/")
    referer = f"{origin}{path}/" if path and path != "/" else f"{origin}/"
    browser_page_url = f"{origin}{path}/desktop" if path and path != "/" else origin
    cookie_domain = parsed.hostname or parsed.netloc.split(":")[0]

    return _BibiRoutes(
        api_base_url=origin,
        referer=referer,
        browser_page_url=browser_page_url,
        origin=origin,
        cookie_domain=cookie_domain,
    )
