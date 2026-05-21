from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from .config import Settings
from .parsers.base import LinkMetadata, MediaType

logger = logging.getLogger(__name__)

_CJK_RE = re.compile(r"[\u3400-\u9fff]")


def contains_chinese(text: str) -> bool:
    return bool(_CJK_RE.search(text))


class TitleTranslator:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    async def translate_metadata(self, meta: LinkMetadata) -> None:
        if not self._settings.title_translation_enabled:
            return
        if not self._settings.deepseek_api_key:
            logger.warning("title translation enabled but deepseek_api_key is empty")
            return

        await self._translate_title_if_needed(meta)
        await self._translate_description_if_needed(meta)

    async def _translate_title(self, title: str) -> str:
        return await self._translate_text(
            title,
            system_prompt=(
                "Translate social media video titles into concise "
                "Simplified Chinese. "
                "Return only the translated title. Do not add explanations."
            ),
            user_prompt=f"Translate this title to Simplified Chinese:\n{title}",
            max_tokens=120,
        )

    async def _translate_description(self, description: str) -> str:
        return await self._translate_text(
            description,
            system_prompt=(
                "Translate social media captions into natural, concise "
                "Simplified Chinese. Preserve names, handles, hashtags, and "
                "meaning. Return only the translation."
            ),
            user_prompt=f"Translate this caption to Simplified Chinese:\n{description}",
            max_tokens=220,
        )

    async def _translate_text(
        self,
        text: str,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
    ) -> str:
        endpoint = self._settings.deepseek_base_url.rstrip("/") + "/chat/completions"
        response = await self._client.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {self._settings.deepseek_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._settings.deepseek_model,
                "messages": [
                    {
                        "role": "system",
                        "content": system_prompt,
                    },
                    {
                        "role": "user",
                        "content": user_prompt,
                    },
                ],
                "temperature": 0.2,
                "max_tokens": max_tokens,
            },
            timeout=self._settings.translation_timeout,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"DeepSeek HTTP {response.status_code}: {response.text[:200]}")

        data: dict[str, Any] = response.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("DeepSeek returned no choices")
        message = choices[0].get("message", {})
        content = str(message.get("content") or "").strip()
        return _clean_translation(content)

    async def _translate_title_if_needed(self, meta: LinkMetadata) -> None:
        if not meta.title.strip():
            return
        if _is_generic_social_title(meta.title, meta.platform):
            return
        if contains_chinese(meta.title):
            return

        try:
            translated = await self._translate_title(meta.title)
        except Exception as e:
            logger.warning(
                "title translation failed: url=%s title=%r error=%s",
                meta.source_url,
                meta.title[:80],
                e,
            )
            return

        if translated and translated != meta.title:
            meta.translated_title = translated

    async def _translate_description_if_needed(self, meta: LinkMetadata) -> None:
        if meta.media_type == MediaType.VIDEO:
            return

        description = meta.description.strip()
        if not description:
            return
        if contains_chinese(description):
            return

        try:
            translated = await self._translate_description(description)
        except Exception as e:
            logger.warning(
                "description translation failed: url=%s description=%r error=%s",
                meta.source_url,
                description[:80],
                e,
            )
            return

        if translated and translated != description:
            meta.translated_description = translated


def _clean_translation(text: str) -> str:
    cleaned = text.strip().strip("\"'")
    cleaned = cleaned.strip(chr(0x201C) + chr(0x201D) + chr(0x2018) + chr(0x2019))
    return " ".join(cleaned.split())


def _is_generic_social_title(title: str, platform: str) -> bool:
    normalized = " ".join(title.strip().lower().split())
    generic_titles = {
        "instagram": {"instagram", "instagram reel", "instagram post"},
        "tiktok": {"tiktok", "tiktok video"},
        "x": {"x", "x video", "x post", "twitter", "twitter video", "twitter post"},
        "bilibili": {"bilibili", "bilibili video"},
        "youtube": {"youtube", "youtube video"},
    }
    return normalized in generic_titles.get(platform.strip().lower(), set())
