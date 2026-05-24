from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
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
        cookie_header = get_cookie_header(
            settings.cookie_file_for_platform("bibigpt"),
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
        """Summarize a video using the chat/completions endpoint.

        Args:
            video_url: URL of the video to summarize.
            prompt: Custom prompt. Falls back to settings.default_prompt.

        Returns:
            SummaryResult with the AI-generated summary.
        """
        if self._cookie_error:
            raise AuthenticationError(0, self._cookie_error)

        effective_prompt = _with_output_instructions(
            prompt or self._settings.bibigpt_default_prompt
        )

        if self._settings.bibigpt_access_mode == "api":
            return await self._summarize_api(video_url, effective_prompt)
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
