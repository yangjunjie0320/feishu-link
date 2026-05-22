from __future__ import annotations

import logging
from typing import Any

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
- Use Markdown formatting for headings, bullet lists, emphasis, and structure."""


class BibiAPIError(Exception):
    """Raised when the BibiGPT API returns an error."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"BibiGPT API error (HTTP {status_code}): {body[:200]}")


class AuthenticationError(BibiAPIError):
    """Raised when cookie authentication fails (401/403)."""


class BibiClient:
    """BibiGPT web API client using cookie-based authentication."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        cookie_header = get_cookie_header(
            settings.cookie_file_for_platform("bibigpt"),
            "bibigpt.co",
        )

        self._headers = {
            "User-Agent": _USER_AGENT,
            "Referer": f"{settings.bibigpt_base_url}/",
            "Origin": settings.bibigpt_base_url,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if cookie_header:
            self._headers["Cookie"] = cookie_header
        logger.info("BibiClient initialized (base_url=%s)", settings.bibigpt_base_url)

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
        effective_prompt = _with_output_instructions(
            prompt or self._settings.bibigpt_default_prompt
        )

        body: dict[str, Any] = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "video_url", "video_url": {"url": video_url}},
                        {"type": "text", "text": effective_prompt},
                    ],
                }
            ],
            "stream": False,
        }

        url = f"{self._settings.bibigpt_base_url}/api/v1/chat/completions"
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

    async def get_user_info(self) -> dict[str, Any]:
        """Fetch current user profile to verify cookie validity."""
        url = f"{self._settings.bibigpt_base_url}/api/trpc/user.me"
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
