from __future__ import annotations

import asyncio
import io
import logging

import httpx
import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody

logger = logging.getLogger(__name__)


async def upload_cover(
    url: str,
    lark_client: lark.Client,
    http_client: httpx.AsyncClient,
) -> str | None:
    try:
        resp = await http_client.get(url, follow_redirects=True, timeout=10)
        resp.raise_for_status()
        image_bytes = resp.content
    except Exception as e:
        logger.warning("cover fetch failed url=%s: %s", url, e)
        return None

    def _upload() -> str | None:
        try:
            request = (
                CreateImageRequest.builder()
                .request_body(
                    CreateImageRequestBody.builder()
                    .image_type("message")
                    .image(io.BytesIO(image_bytes))
                    .build()
                )
                .build()
            )
            response = lark_client.im.v1.image.create(request)
            if response.success():
                return response.data.image_key
            logger.warning("image upload failed: %s %s", response.code, response.msg)
            return None
        except Exception as e:
            logger.warning("image upload failed url=%s: %s", url, e)
            return None

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _upload)
