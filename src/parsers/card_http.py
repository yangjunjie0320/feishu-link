from __future__ import annotations

import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from ..card_metadata import CardSourceError
from ..http_errors import describe_request_error


async def get_response(
    client: httpx.AsyncClient, endpoint: str, *, source_url: str, label: str,
    headers: dict[str, str] | None = None, params: dict[str, str] | None = None,
) -> httpx.Response:
    try:
        response = await client.get(
            endpoint, headers=headers, params=params, follow_redirects=True,
        )
    except httpx.RequestError as exc:
        raise CardSourceError(
            source_url, f"{label} request error: {describe_request_error(exc)}", kind="network",
        ) from exc
    if response.status_code >= 400:
        body = response.text[:500].lower()
        challenge = any(value in body for value in (
            "checkpoint_required", "challenge_required", "captcha_required",
        ))
        auth = response.status_code == 401 or any(value in body for value in (
            "login_required", "login required", "session expired", "not authenticated",
        ))
        kind = "challenge" if challenge else ("auth" if auth else (
            "rate_limit" if response.status_code == 429 else (
                "network" if response.status_code >= 500 else "content"
            )
        ))
        raise CardSourceError(
            source_url, f"{label} HTTP {response.status_code}", kind=kind,
            retry_after=_retry_after(response) if kind == "rate_limit" else None,
        )
    return response


def json_object(response: httpx.Response, url: str, label: str) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise CardSourceError(url, f"{label} returned non-json response") from exc
    if not isinstance(data, dict):
        raise CardSourceError(url, f"{label} returned invalid response object")
    messages = [data.get("message")]
    errors = data.get("errors")
    if isinstance(errors, list):
        messages.extend(error.get("message") for error in errors if isinstance(error, dict))
    for message in messages:
        message = message.lower() if isinstance(message, str) else ""
        if any(signal in message for signal in (
            "checkpoint_required", "challenge_required", "captcha_required",
        )):
            raise CardSourceError(url, f"{label} verification required", kind="challenge")
        if any(signal in message for signal in (
            "login_required", "login required", "could not authenticate you",
            "invalid or expired token", "not authenticated",
        )):
            raise CardSourceError(url, f"{label} login required", kind="auth")
        if "rate limit" in message or "please wait a few minutes" in message:
            raise CardSourceError(
                url, f"{label} rate limited", kind="rate_limit", retry_after=_retry_after(response),
            )
    return data


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after", "")
    try:
        delay = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            delay = (retry_at - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, delay) if math.isfinite(delay) else None
