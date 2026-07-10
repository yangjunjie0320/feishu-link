from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import httpx

from .bibi_models import SubtitleSegment
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
_SUBTITLE_FORMAT_SYSTEM_PROMPT = """\
你是视频字幕校对与翻译助手。必须只输出一个有效的 JSON 对象，格式为
{"items":[{"id":数字,"text":"字幕文本"}]}。
逐条忠实处理输入字幕：非中文翻译为简体中文，只修正标点、断句和明显错别字。
不得总结、删减、合并、拆分、补充事实或改变原意；必须保留每一条输入的 id、顺序和数量。
text 必须是非空字符串。不要输出时间戳、Markdown、解释或 JSON 之外的内容。"""
_SUBTITLE_BATCH_MAX_ITEMS = 80
_SUBTITLE_BATCH_MAX_CHARACTERS = 6000
_SUBTITLE_MAX_TOKENS = 8192
_SUBTITLE_RESPONSE_ATTEMPTS = 2


class _SubtitleResponseError(ValueError):
    """DeepSeek returned a response that cannot safely replace a subtitle batch."""


class _SubtitleServiceError(RuntimeError):
    """DeepSeek could not serve a subtitle formatting request."""


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
            logger.warning("summary rewrite failed: error=%s", e)
            return markdown

        if _has_section_titles(result):
            return result

        logger.warning("summary rewrite produced no section titles, retrying")
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
            logger.warning("summary rewrite retry failed: error=%s", e)

        return result

    async def format_subtitles(
        self,
        subtitles: Sequence[SubtitleSegment],
        *,
        content_id: str = "",
    ) -> tuple[SubtitleSegment, ...]:
        """Translate and proofread subtitles without exposing their timing to the model."""
        original = tuple(subtitles)
        if not original:
            return ()
        if not self._settings.deepseek_api_key:
            logger.warning(
                "subtitle formatting skipped because deepseek_api_key is empty: "
                "content_id=%s segments=%d",
                content_id,
                len(original),
            )
            return original

        batches = _build_subtitle_batches(original)
        formatted: list[SubtitleSegment] = []
        fallback_batches = 0

        for batch_number, batch in enumerate(batches, start=1):
            character_count = sum(len(segment.text) for segment in batch)
            if character_count > _SUBTITLE_BATCH_MAX_CHARACTERS:
                logger.warning(
                    "subtitle batch exceeds formatting character limit, using original text: "
                    "content_id=%s batch=%d/%d segments=%d characters=%d",
                    content_id,
                    batch_number,
                    len(batches),
                    len(batch),
                    character_count,
                )
                formatted.extend(batch)
                fallback_batches += 1
                continue

            batch_result: tuple[SubtitleSegment, ...] | None = None
            for attempt in range(1, _SUBTITLE_RESPONSE_ATTEMPTS + 1):
                try:
                    batch_result = await self._format_subtitle_batch(batch)
                    break
                except _SubtitleResponseError as error:
                    if attempt < _SUBTITLE_RESPONSE_ATTEMPTS:
                        logger.warning(
                            "subtitle formatting response invalid, retrying: "
                            "content_id=%s batch=%d/%d attempt=%d error=%s",
                            content_id,
                            batch_number,
                            len(batches),
                            attempt,
                            error,
                        )
                        continue
                    logger.warning(
                        "subtitle formatting response invalid, using original batch: "
                        "content_id=%s batch=%d/%d attempts=%d error=%s",
                        content_id,
                        batch_number,
                        len(batches),
                        attempt,
                        error,
                    )
                except Exception as error:
                    logger.warning(
                        "subtitle formatting service failed, using original remaining batches: "
                        "content_id=%s batch=%d/%d error=%s",
                        content_id,
                        batch_number,
                        len(batches),
                        error,
                    )
                    formatted.extend(batch)
                    for remaining_batch in batches[batch_number:]:
                        formatted.extend(remaining_batch)
                    fallback_batches += len(batches) - batch_number + 1
                    logger.info(
                        "subtitle formatting completed with service fallback: "
                        "content_id=%s segments=%d batches=%d fallback_batches=%d",
                        content_id,
                        len(original),
                        len(batches),
                        fallback_batches,
                    )
                    return tuple(formatted)

            if batch_result is None:
                formatted.extend(batch)
                fallback_batches += 1
            else:
                formatted.extend(batch_result)

        logger.info(
            "subtitle formatting completed: "
            "content_id=%s segments=%d batches=%d fallback_batches=%d",
            content_id,
            len(original),
            len(batches),
            fallback_batches,
        )
        return tuple(formatted)

    async def _format_subtitle_batch(
        self, batch: Sequence[SubtitleSegment]
    ) -> tuple[SubtitleSegment, ...]:
        input_items = [{"id": segment.index, "text": segment.text} for segment in batch]
        payload = self._build_chat_payload(
            _SUBTITLE_FORMAT_SYSTEM_PROMPT,
            (
                "请按系统要求处理下面的 JSON 字幕。只返回 JSON 对象：\n"
                + json.dumps({"items": input_items}, ensure_ascii=False, separators=(",", ":"))
            ),
            _SUBTITLE_MAX_TOKENS,
        )
        payload["thinking"] = {"type": "disabled"}
        payload["response_format"] = {"type": "json_object"}

        endpoint = self._settings.deepseek_base_url.rstrip("/") + "/chat/completions"
        response = await self._client.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {self._settings.deepseek_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self._settings.summary_rewrite_timeout,
        )
        if not 200 <= response.status_code < 300:
            raise _SubtitleServiceError(f"DeepSeek HTTP {response.status_code}")

        content = _extract_subtitle_response_content(response)
        return _parse_formatted_subtitle_batch(content, batch)

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
                "title translation failed: title=%r error=%s",
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
                "description translation failed: description=%r error=%s",
                description[:80],
                e,
            )
            return

        if translated and translated != description:
            meta.translated_description = translated


def _build_subtitle_batches(
    subtitles: Sequence[SubtitleSegment],
) -> tuple[tuple[SubtitleSegment, ...], ...]:
    batches: list[tuple[SubtitleSegment, ...]] = []
    current: list[SubtitleSegment] = []
    current_characters = 0

    for segment in subtitles:
        segment_characters = len(segment.text)
        would_exceed_items = len(current) >= _SUBTITLE_BATCH_MAX_ITEMS
        would_exceed_characters = (
            bool(current)
            and current_characters + segment_characters > _SUBTITLE_BATCH_MAX_CHARACTERS
        )
        if would_exceed_items or would_exceed_characters:
            batches.append(tuple(current))
            current = []
            current_characters = 0

        current.append(segment)
        current_characters += segment_characters

    if current:
        batches.append(tuple(current))
    return tuple(batches)


def _extract_subtitle_response_content(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError as error:
        raise _SubtitleResponseError("response body is not valid JSON") from error
    if not isinstance(data, dict):
        raise _SubtitleResponseError("response body is not a JSON object")

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise _SubtitleResponseError("response has no valid choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise _SubtitleResponseError("response choice has no valid message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise _SubtitleResponseError("response message has empty content")
    return content.strip()


def _parse_formatted_subtitle_batch(
    content: str, batch: Sequence[SubtitleSegment]
) -> tuple[SubtitleSegment, ...]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as error:
        raise _SubtitleResponseError("message content is not valid JSON") from error
    if not isinstance(data, dict):
        raise _SubtitleResponseError("message content is not a JSON object")
    items = data.get("items")
    if not isinstance(items, list):
        raise _SubtitleResponseError("message content has no items array")
    if len(items) != len(batch):
        raise _SubtitleResponseError("message item count does not match input")

    formatted: list[SubtitleSegment] = []
    for item, segment in zip(items, batch, strict=True):
        if not isinstance(item, dict):
            raise _SubtitleResponseError("message item is not a JSON object")
        item_id = item.get("id")
        if type(item_id) is not int or item_id != segment.index:
            raise _SubtitleResponseError("message item ids do not match input order")
        text = item.get("text")
        if not isinstance(text, str):
            raise _SubtitleResponseError("message item text is not a string")
        cleaned_text = " ".join(text.split())
        if not cleaned_text:
            raise _SubtitleResponseError("message item text is empty")
        formatted.append(replace(segment, text=cleaned_text))
    return tuple(formatted)


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
