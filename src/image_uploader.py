from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx
import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody

from .feishu_async import call_feishu_async

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CoverUploadResult:
    image_key: str | None
    status: str
    attempted: int
    reason: str = ""


async def upload_cover(
    url: str,
    lark_client: lark.Client,
    http_client: httpx.AsyncClient,
    *,
    candidates: Sequence[str] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 10.0,
) -> str | None:
    """Compatibility wrapper; callers needing a reason can use the result API."""
    result = await upload_cover_with_result(
        url, lark_client, http_client, candidates=candidates, headers=headers, timeout=timeout
    )
    return result.image_key


async def upload_cover_with_result(
    url: str,
    lark_client: lark.Client,
    http_client: httpx.AsyncClient,
    *,
    candidates: Sequence[str] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 10.0,
) -> CoverUploadResult:
    """Try at most three distinct images within one download/upload budget.

    Signed URL query strings and source headers are passed through unchanged.
    Logs report candidate position, stage and error type, never headers or URLs.
    """
    urls: list[str] = []
    for candidate in (url, *(candidates or ())):
        try:
            normalized = _normalize_cover_url(candidate)
            parsed = urlsplit(normalized)
        except ValueError:
            continue
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or normalized in urls:
            continue
        urls.append(normalized)
        if len(urls) == 3:
            break
    if not urls:
        return CoverUploadResult(None, "unavailable", 0, "no valid cover candidate")

    deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
    attempted = 0
    last_reason = "cover budget exhausted"
    last_status = "timeout"
    try:
        async with asyncio.timeout(timeout):
            for index, candidate in enumerate(urls):
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                # Leave another candidate a chance if this CDN stalls.
                attempt_timeout = remaining / (len(urls) - index)
                attempted += 1
                stage = "fetch"
                try:
                    async with asyncio.timeout(attempt_timeout):
                        response = await http_client.get(
                            candidate,
                            headers=dict(headers or {}),
                            follow_redirects=True,
                            timeout=attempt_timeout,
                        )
                        response.raise_for_status()
                        image_bytes = response.content
                        content_type = response.headers.get("content-type", "").lower()
                        if not image_bytes or (
                            content_type
                            and not content_type.startswith("image/")
                            and "application/octet-stream" not in content_type
                        ):
                            raise ValueError("cover response is empty or not an image")
                        stage = "upload"
                        with io.BytesIO(image_bytes) as image:
                            request = (
                                CreateImageRequest.builder()
                                .request_body(
                                    CreateImageRequestBody.builder()
                                    .image_type("message")
                                    .image(image)
                                    .build()
                                )
                                .build()
                            )
                            uploaded = await call_feishu_async(
                                lark_client,
                                "image",
                                "acreate",
                                request,
                                timeout=attempt_timeout,
                            )
                        if not uploaded.success():
                            last_reason = f"upload rejected: code={uploaded.code}"
                            last_status = "failed"
                        else:
                            image_key = getattr(uploaded.data, "image_key", None)
                            if isinstance(image_key, str) and image_key:
                                logger.info("cover uploaded: candidate=%d/%d", attempted, len(urls))
                                return CoverUploadResult(image_key, "uploaded", attempted)
                            last_reason = "upload returned no image_key"
                            last_status = "failed"
                except TimeoutError:
                    last_reason = f"{stage} timed out"
                    last_status = "timeout"
                except httpx.HTTPStatusError as exc:
                    last_reason = f"{stage} HTTP {exc.response.status_code}"
                    last_status = "failed"
                except Exception as exc:
                    last_reason = f"{stage} failed: {type(exc).__name__}"
                    last_status = "failed"
                logger.warning(
                    "cover candidate failed: candidate=%d/%d reason=%s",
                    attempted,
                    len(urls),
                    last_reason,
                )
    except TimeoutError:
        last_reason = "cover total budget exhausted"
        last_status = "timeout"
    logger.warning("cover unavailable: attempted=%d reason=%s", attempted, last_reason)
    return CoverUploadResult(None, last_status, attempted, last_reason)


def _normalize_cover_url(url: str) -> str:
    normalized = url.strip()
    if not normalized:
        return ""

    parsed = urlsplit(normalized)
    if not parsed.scheme and parsed.netloc:
        return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, parsed.fragment))
    return normalized
