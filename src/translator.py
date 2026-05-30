from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from .config import Settings
from .parsers.base import LinkMetadata, MediaType

logger = logging.getLogger(__name__)

_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_SUMMARY_REWRITE_PROMPT = """\
输出要求:
- 必须用简体中文输出, 非中文内容要翻译成中文。不要使用 emoji。
- 使用 Markdown，尽量保留原有结构。
- Markdown 各级标题都改成无序列表+加粗。
- 允许使用多级无序列表，用缩进表达层级。
- 无序列表只能使用 "-", 不要使用 "*" 或 "+"。
- 不要使用编号列表。
- 标签：从内容中提取 3-8 个关键标签，格式为 "`#标签`"，用空格分隔，作为单独一行放在所有章节之前。
- 章节排列顺序：总结/概述 → 要点/亮点 → 详细内容/章节细分 → 问题/FAQ → 词汇/术语/补充。
- 不得合并或重命名章节；原文中没有对应内容的章节直接省略，不要写"无"或任何占位符。"""


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

    async def ensure_chinese_markdown_summary(
        self,
        markdown: str,
        *,
        source_url: str,
        rewrite_prompt: str | None = None,
    ) -> str:
        content = markdown.strip()
        if not content:
            return markdown
        if not self._settings.deepseek_api_key:
            logger.warning("summary rewrite skipped because deepseek_api_key is empty")
            return markdown

        prompt = (rewrite_prompt or _SUMMARY_REWRITE_PROMPT).strip()
        system_prompt = (
            "你是视频总结重写助手。不要信任输入的语言和排版。"
            "按用户给定要求把 BibiGPT 原始返回重写为最终内容。"
            "只返回重写后的正文。"
        )
        rewrite_timeout = self._settings.summary_rewrite_timeout
        try:
            result = await self._translate_text(
                content,
                system_prompt=system_prompt,
                user_prompt=(
                    "重写要求:\n"
                    f"{prompt}\n\n"
                    "BibiGPT 原始返回:\n"
                    f"{content}"
                ),
                max_tokens=1200,
                preserve_linebreaks=True,
                timeout=rewrite_timeout,
            )
        except Exception as e:
            logger.warning("summary rewrite failed: url=%s error=%s", source_url, e)
            return markdown

        if _has_section_titles(result):
            return result

        logger.warning(
            "summary rewrite produced no section titles, retrying: url=%s", source_url
        )
        try:
            result = await self._translate_text(
                content,
                system_prompt=system_prompt,
                user_prompt=(
                    "重写要求:\n"
                    f"{prompt}\n\n"
                    "注意：每个章节标题必须格式为 \"- **标题**\"，不要用纯文字标题。\n\n"
                    "BibiGPT 原始返回:\n"
                    f"{content}"
                ),
                max_tokens=1200,
                preserve_linebreaks=True,
                timeout=rewrite_timeout,
            )
        except Exception as e:
            logger.warning("summary rewrite retry failed: url=%s error=%s", source_url, e)

        return result

    def _build_chat_payload(
        self, system_prompt: str, user_prompt: str, max_tokens: int
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": self._settings.deepseek_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        if not self._settings.deepseek_thinking_enabled:
            payload["thinking"] = {"type": "disabled"}
        elif self._settings.deepseek_reasoning_effort is not None:
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = self._settings.deepseek_reasoning_effort
        return payload

    async def _translate_text(
        self,
        text: str,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        preserve_linebreaks: bool = False,
        timeout: float | None = None,
    ) -> str:
        endpoint = self._settings.deepseek_base_url.rstrip("/") + "/chat/completions"
        response = await self._client.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {self._settings.deepseek_api_key}",
                "Content-Type": "application/json",
            },
            json=self._build_chat_payload(system_prompt, user_prompt, max_tokens),
            timeout=timeout if timeout is not None else self._settings.translation_timeout,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"DeepSeek HTTP {response.status_code}: {response.text[:200]}")

        data: dict[str, Any] = response.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("DeepSeek returned no choices")
        message = choices[0].get("message", {})
        content = str(message.get("content") or "").strip()
        if preserve_linebreaks:
            return _clean_markdown_translation(content)
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


def _clean_markdown_translation(text: str) -> str:
    cleaned = text.strip().strip("\"'")
    cleaned = cleaned.strip(chr(0x201C) + chr(0x201D) + chr(0x2018) + chr(0x2019))
    lines = [_clean_markdown_translation_line(line) for line in cleaned.splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _clean_markdown_translation_line(line: str) -> str:
    expanded = line.expandtabs(4).rstrip()
    stripped = expanded.lstrip(" ")
    if not stripped:
        return ""
    indent = len(expanded) - len(stripped)
    return (" " * indent) + " ".join(stripped.split())


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


_SECTION_TITLE_RE = re.compile(r"^- \*\*.+\*\*", re.MULTILINE)


def _has_section_titles(markdown: str) -> bool:
    return len(_SECTION_TITLE_RE.findall(markdown)) >= 2
