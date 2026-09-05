from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import OrderedDict
from typing import Any

import httpx
import lark_oapi as lark

from .archive_store import ArchiveEntry, BibiLinkUpdate, BitableArchive, RemarkAppend
from .bibi_client import (
    AuthenticationError,
    BibiAPIError,
    BibiClient,
    TranscriptUnavailableError,
    share_page_url,
)
from .bibi_models import SummaryResult
from .card import (
    build_card,
    build_chapter_summary_cards,
    build_markdown_card,
    card_message_wire_size,
)
from .comment_analyzer import CommentAnalysisError, CommentAnalyzer
from .config import Settings
from .dispatch import Dispatcher
from .image_uploader import upload_cover
from .listener import CardActionEvent, ListenerEvent, MessageEvent
from .media_downloader import VideoSkipReason, download_video
from .parsers.base import LinkMetadata, ParserError
from .platforms import is_short_video_platform
from .sender import CardSender, TextSender, TypingReactionSender, VideoSender
from .translator import TitleTranslator
from .url_extract import (
    decode_plain_text,
    extract_prompt,
    extract_urls,
    is_manual_download_command,
)

logger = logging.getLogger(__name__)

_SEEN_CAPACITY = 500
_CHAPTER_SUMMARY_SEND_INTERVAL_SECONDS = 0.25


def _remember(cache: OrderedDict[str, Any], key: str, value: Any) -> None:
    cache[key] = value
    cache.move_to_end(key)
    if len(cache) > _SEEN_CAPACITY:
        cache.popitem(last=False)


class SummaryCooldownError(Exception):
    """The same video failed moments ago; refusing to hit BibiGPT again yet."""

    def __init__(self, remaining_seconds: int, reason: str) -> None:
        self.remaining_seconds = remaining_seconds
        self.reason = reason
        super().__init__(f"summary cooling down for {remaining_seconds}s: {reason}")


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        lark_client: lark.Client,
        archive: BitableArchive | None = None,
    ) -> None:
        self._settings = settings
        self._archive = archive
        self._http = httpx.AsyncClient(timeout=settings.request_timeout)
        self._dispatcher = Dispatcher(settings, self._http)
        self._translator = TitleTranslator(settings, self._http)
        self._sender = CardSender(settings, lark_client)
        self._text_sender = TextSender(settings, lark_client)
        self._video_sender = VideoSender(settings, lark_client)
        self._typing_sender = TypingReactionSender(lark_client)
        self._bibi_client = BibiClient(settings)
        self._comment_analyzer = CommentAnalyzer(settings, self._http)
        self._lark_client = lark_client
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._bot_open_id: str | None = None
        # Reply-remark and BibiGPT-link lookups: original message_id -> the URL
        # archived for it, and meta.source_url -> that same archived URL (they
        # differ when canonical_url rewrites a short link like b23.tv).
        self._msg_archive_url: OrderedDict[str, str] = OrderedDict()
        self._source_archive_url: OrderedDict[str, str] = OrderedDict()
        # url -> contentId of an already-generated summary; its presence is
        # the duplicate-summarize marker that switches to the cached lookup.
        self._bibigpt_content_id: OrderedDict[str, str] = OrderedDict()
        # url -> (initiating message_id, pending result) for default-prompt
        # summaries still being generated, so repeated requests share one run.
        self._inflight_summaries: dict[str, tuple[str, asyncio.Future[SummaryResult]]] = {}
        # Default-prompt results kept for a while so a re-click reuses them
        # without another browser run: url -> (expires_at, result). And after a
        # failed run, url -> (expires_at, user-facing reason): requests inside
        # that window are answered from here instead of hitting BibiGPT again.
        self._recent_summaries: OrderedDict[str, tuple[float, SummaryResult]] = OrderedDict()
        self._summary_failures: OrderedDict[str, tuple[float, str]] = OrderedDict()

    async def handle(self, event: ListenerEvent) -> None:
        if isinstance(event, CardActionEvent):
            await self._handle_card_action(event)
            return

        if event.message_id in self._seen:
            logger.debug("skip duplicate message_id=%s", event.message_id)
            return
        self._seen[event.message_id] = None
        if len(self._seen) > _SEEN_CAPACITY:
            self._seen.popitem(last=False)

        is_bot_mentioned = False
        if event.mentions:
            if self._bot_open_id is None:
                self._bot_open_id = await self._fetch_bot_open_id()
            if self._bot_open_id in event.mentions:
                is_bot_mentioned = True
                logger.debug("message @mentions the bot itself, message_id=%s", event.message_id)

        urls = extract_urls(event.message_type, event.content, self._settings)
        if not urls:
            await self._maybe_handle_reply_remark(event)
            return
        if len(urls) > 1:
            logger.info(
                "skip: %d URLs in one message, message_id=%s",
                len(urls),
                event.message_id,
            )
            return

        manual_download = is_bot_mentioned and is_manual_download_command(
            event.message_type,
            event.content,
        )
        logger.info(
            "processing %d URL(s) from message_id=%s manual_download=%s",
            len(urls),
            event.message_id,
            manual_download,
        )

        for url in urls:
            await self._process_url(url, event, is_bot_mentioned, manual_download)

    async def _handle_card_action(self, event: CardActionEvent) -> None:
        if event.action == "summarize_video":
            logger.info(
                "processing card summary action: url=%s message_id=%s operator=%s",
                event.source_url,
                event.message_id,
                event.operator_open_id,
            )
            await self._try_send_bibigpt_summary(event.source_url, event)
            return

        if event.action == "analyze_comments":
            logger.info(
                "processing card comment analysis action: url=%s message_id=%s operator=%s",
                event.source_url,
                event.message_id,
                event.operator_open_id,
            )
            await self._try_send_comment_analysis(event.source_url, event)
            return

        if event.action != "download_video":
            logger.info(
                "ignore unsupported card action: action=%s message_id=%s",
                event.action,
                event.message_id,
            )
            return

        logger.info(
            "processing card download action: url=%s message_id=%s operator=%s",
            event.source_url,
            event.message_id,
            event.operator_open_id,
        )
        async with self._typing_sender.hold(event.message_id, label="download"):
            try:
                meta = await self._dispatcher.parse(event.source_url)
            except ParserError as e:
                logger.error(
                    "card action parse failed: url=%s message_id=%s reason=%s",
                    event.source_url,
                    event.message_id,
                    e.reason,
                )
                await self._text_sender.send(
                    _download_failure_message(event.source_url, f"链接解析失败: {e.reason}"),
                    event.chat_id,
                    event.message_id,
                )
                return

            img_key: str | None = None
            if meta.cover_url:
                img_key = await upload_cover(meta.cover_url, self._lark_client, self._http)

            await self._try_send_video(
                meta,
                event,
                img_key,
                notify_user=True,
                enforce_duration_limit=False,
                show_typing=False,
            )

    async def _maybe_handle_reply_remark(self, event: MessageEvent) -> None:
        """A URL-less message replying (thread or quote) to an archived link
        message or to the bot's card gets its text appended to the archived
        row's remark column. Anything unresolvable is a silent skip: replies
        about unrelated messages are normal chat, not an error."""
        if (
            self._archive is None
            or event.message_type not in ("text", "post")
            or not (event.root_id or event.parent_id)
        ):
            logger.debug("skip: no URLs in message_id=%s", event.message_id)
            return
        text = decode_plain_text(event.message_type, event.content)
        if not text:
            return
        archive_url = await self._resolve_replied_archive_url(event)
        if not archive_url:
            logger.debug(
                "reply does not target an archived link: message_id=%s root_id=%s parent_id=%s",
                event.message_id,
                event.root_id,
                event.parent_id,
            )
            return
        self._archive.enqueue(
            RemarkAppend(
                url=archive_url,
                text=text,
                sender_open_id=event.sender_id,
                chat_id=event.chat_id,
                chat_type=event.chat_type,
                message_id=event.message_id,
            )
        )
        logger.info("reply remark enqueued: url=%s message_id=%s", archive_url, event.message_id)

    async def _resolve_replied_archive_url(self, event: MessageEvent) -> str | None:
        """root_id points at the first message of the reply chain, i.e. the
        original link message even when the reply targets the bot's card, so
        the in-process map filled at card-send time answers most lookups. The
        message-read API is the fallback for replies to cards sent before a
        restart; it needs a message read scope and degrades to a WARNING."""
        ref_ids = [i for i in dict.fromkeys((event.root_id, event.parent_id)) if i]
        for ref_id in ref_ids:
            cached = self._msg_archive_url.get(ref_id)
            if cached:
                return cached
        for ref_id in ref_ids:
            url = await self._fetch_replied_url(ref_id)
            if url:
                return self._source_archive_url.get(url, url)
        return None

    async def _fetch_replied_url(self, message_id: str) -> str | None:
        request = (
            lark.BaseRequest.builder()
            .http_method(lark.HttpMethod.GET)
            .uri(f"/open-apis/im/v1/messages/{message_id}")
            .token_types({lark.AccessTokenType.TENANT})
            .build()
        )
        try:
            resp = await self._lark_client.arequest(request)
            if not resp.success():
                logger.warning(
                    "failed to fetch replied message (missing im:message read scope?): "
                    "message_id=%s code=%s msg=%s",
                    message_id,
                    resp.code,
                    resp.msg,
                )
                return None
            data = json.loads(resp.raw.content.decode("utf-8")).get("data") or {}
        except Exception as e:
            logger.warning("failed to fetch replied message: message_id=%s error=%s", message_id, e)
            return None
        # Text/post messages decode normally; interactive card content falls
        # through extract_urls' raw path where the URL regex still applies.
        for item in data.get("items") or []:
            content = str((item.get("body") or {}).get("content") or "")
            msg_type = str(item.get("msg_type") or "")
            for url in extract_urls(msg_type, content, self._settings):
                if is_short_video_platform(url):
                    return url
        return None

    async def _process_url(
        self,
        url: str,
        event: MessageEvent,
        is_bot_mentioned: bool,
        manual_download: bool,
    ) -> None:
        if not is_short_video_platform(url):
            logger.debug("skip unsupported platform url=%s", url)
            return
        logger.debug("parsing url=%s", url)
        async with self._typing_sender.hold(event.message_id, label="card"):
            try:
                meta = await self._dispatcher.parse(url)
            except ParserError as e:
                logger.error(
                    "parse failed: url=%s message_id=%s reason=%s",
                    url,
                    event.message_id,
                    e.reason,
                )
                if manual_download:
                    await self._text_sender.send(
                        _download_failure_message(url, f"链接解析失败: {e.reason}"),
                        event.chat_id,
                        event.message_id,
                    )
                return

            await self._translator.translate_metadata(meta)

            img_key: str | None = None
            if meta.cover_url:
                img_key = await upload_cover(meta.cover_url, self._lark_client, self._http)

            card_json = build_card(meta, img_key)
            card_sent = await self._sender.send(card_json, event.chat_id, event.message_id)
            if not card_sent:
                logger.error(
                    "skip video append because card send failed: url=%s message_id=%s",
                    url,
                    event.message_id,
                )
                return
            logger.info(
                "card sent: url=%s title=%r message_id=%s",
                url,
                meta.title[:50] if meta.title else "",
                event.message_id,
            )

            if self._archive is not None:
                archive_url = meta.canonical_url or meta.source_url
                self._archive.enqueue(
                    ArchiveEntry(
                        title=meta.translated_title or meta.title,
                        url=archive_url,
                        platform=meta.platform,
                        channel=meta.channel or "",
                        duration_seconds=meta.duration_seconds,
                        sender_open_id=event.sender_id,
                        chat_id=event.chat_id,
                        chat_type=event.chat_type,
                    )
                )
                _remember(self._msg_archive_url, event.message_id, archive_url)
                _remember(self._source_archive_url, meta.source_url, archive_url)

        domain = ""
        url_lower = url.lower()
        if "youtube.com" in url_lower or "youtu.be" in url_lower:
            domain = "youtube"
        elif "bilibili.com" in url_lower or "b23.tv" in url_lower:
            domain = "bilibili"

        if manual_download:
            await self._try_send_video(
                meta,
                event,
                img_key,
                notify_user=True,
                enforce_duration_limit=False,
            )
        elif is_bot_mentioned and domain in ("youtube", "bilibili"):
            await self._try_send_bibigpt_summary(url, event)
        else:
            await self._try_send_video(meta, event, img_key)

    async def _try_send_video(
        self,
        meta: LinkMetadata,
        event: MessageEvent | CardActionEvent,
        img_key: str | None,
        *,
        notify_user: bool = False,
        enforce_duration_limit: bool = True,
        show_typing: bool = True,
        typing_label: str = "video",
    ) -> None:
        if show_typing:
            async with self._typing_sender.hold(event.message_id, label=typing_label):
                await self._download_and_send_video(
                    meta,
                    event,
                    img_key,
                    notify_user=notify_user,
                    enforce_duration_limit=enforce_duration_limit,
                )
            return

        await self._download_and_send_video(
            meta,
            event,
            img_key,
            notify_user=notify_user,
            enforce_duration_limit=enforce_duration_limit,
        )

    async def _download_and_send_video(
        self,
        meta: LinkMetadata,
        event: MessageEvent | CardActionEvent,
        img_key: str | None,
        *,
        notify_user: bool,
        enforce_duration_limit: bool,
    ) -> None:
        try:
            video = await download_video(
                meta,
                self._settings,
                enforce_duration_limit=enforce_duration_limit,
            )
        except VideoSkipReason as e:
            logger.info(
                "skip video append: url=%s message_id=%s reason=%s",
                meta.source_url,
                event.message_id,
                e,
            )
            if notify_user:
                await self._text_sender.send(
                    _download_failure_message(meta.source_url, str(e)),
                    event.chat_id,
                    event.message_id,
                )
            return
        except Exception as e:
            logger.warning(
                "video download failed: url=%s message_id=%s error=%s",
                meta.source_url,
                event.message_id,
                e,
            )
            if notify_user:
                error = str(e) or e.__class__.__name__
                await self._text_sender.send(
                    _download_failure_message(meta.source_url, error),
                    event.chat_id,
                    event.message_id,
                )
            return

        try:
            await self._video_sender.send(
                video.path,
                video.file_name,
                video.duration_ms,
                event.chat_id,
                event.message_id,
                img_key,
            )
        except Exception as e:
            logger.warning(
                "video send failed: url=%s message_id=%s file_name=%s error=%s",
                meta.source_url,
                event.message_id,
                video.file_name,
                e,
            )
            if notify_user:
                error = str(e) or e.__class__.__name__
                await self._text_sender.send(
                    _download_failure_message(meta.source_url, f"发送失败: {error}"),
                    event.chat_id,
                    event.message_id,
                )
        finally:
            video.cleanup()

    async def _try_send_bibigpt_summary(
        self,
        url: str,
        event: MessageEvent | CardActionEvent,
    ) -> None:
        async with self._typing_sender.hold(event.message_id, label="bibigpt"):
            prompt = (
                extract_prompt(event.message_type, event.content, url)
                if isinstance(event, MessageEvent)
                else None
            )
            logger.info(
                "summarizing video prompt=%s message_id=%s",
                prompt[:30] if prompt else "(default)",
                event.message_id,
            )

            try:
                if prompt and prompt.strip():
                    result = await self._bibi_client.summarize(url, prompt=prompt)
                else:
                    result = await self._shared_default_summary(url, event.message_id)
                    if result is None:
                        return
                summary_content = await self._translator.ensure_chinese_markdown_summary(
                    result.content,
                    source_url=url,
                )
            except Exception as e:
                if isinstance(e, SummaryCooldownError):
                    logger.info(
                        "summary cooling down: message_id=%s remaining=%ds",
                        event.message_id,
                        e.remaining_seconds,
                    )
                else:
                    logger.error(
                        "summarize failed: message_id=%s error_type=%s status=%s reason=%s",
                        event.message_id,
                        e.__class__.__name__,
                        getattr(e, "status_code", "n/a"),
                        str(e)[:300],
                    )
                await self._text_sender.send(
                    _summary_failure_message(e),
                    event.chat_id,
                    event.message_id,
                )
                return

            bibigpt_url = share_page_url(self._settings.bibigpt_base_url, url, result.content_id)
            try:
                card_json = build_markdown_card(
                    "BibiGPT 总结",
                    summary_content,
                    source_url=url,
                    collapsed=True,
                    panel_title="总结正文",
                    sectioned=True,
                    extra_links=[("BibiGPT 页面", bibigpt_url)],
                )
                card_sent = await self._sender.send(card_json, event.chat_id, event.message_id)
                if not card_sent:
                    logger.error(
                        "summary card send failed: message_id=%s",
                        event.message_id,
                    )
                    return
                logger.info(
                    "summary sent: content_id=%s tokens=%d cached=%s message_id=%s",
                    result.content_id or "missing",
                    result.usage.total_tokens,
                    result.from_cache,
                    event.message_id,
                )
            except Exception as e:
                logger.error(
                    "summary reply failed: message_id=%s error=%s",
                    event.message_id,
                    e,
                )
                return

            if result.content_id:
                _remember(self._bibigpt_content_id, url, result.content_id)
            if self._archive is not None:
                self._archive.enqueue(
                    BibiLinkUpdate(
                        url=self._source_archive_url.get(url, url),
                        bibigpt_url=bibigpt_url,
                    )
                )

            await self._try_send_bibigpt_chapter_summary(event, result)

    async def _shared_default_summary(self, url: str, message_id: str) -> SummaryResult | None:
        """Default-prompt summary with in-flight de-duplication. Concurrent
        requests for the same URL wait on the first run's result instead of
        launching another browser; a repeat from the very same message is
        dropped (None) because the first run already replies to that thread."""
        now = time.monotonic()
        cached = self._recent_summaries.get(url)
        if cached is not None:
            expires_at, cached_result = cached
            if expires_at > now:
                logger.info("using recent summary cache: url=%s message_id=%s", url, message_id)
                return cached_result
            self._recent_summaries.pop(url, None)
        failed = self._summary_failures.get(url)
        if failed is not None:
            expires_at, reason = failed
            if expires_at > now:
                raise SummaryCooldownError(int(expires_at - now) + 1, reason)
            self._summary_failures.pop(url, None)

        inflight = self._inflight_summaries.get(url)
        if inflight is not None:
            owner_message_id, future = inflight
            if owner_message_id == message_id:
                logger.info(
                    "summary already in progress, ignoring repeat: message_id=%s", message_id
                )
                return None
            logger.info(
                "summary in progress for the same url, waiting: message_id=%s owner=%s",
                message_id,
                owner_message_id,
            )
            return await future

        future: asyncio.Future[SummaryResult] = asyncio.get_running_loop().create_future()
        self._inflight_summaries[url] = (message_id, future)
        try:
            result = await self._lookup_cached_summary(url, None, message_id)
            if result is None:
                result = await self._bibi_client.summarize(url, prompt=None)
        except BaseException as e:
            if isinstance(e, Exception):
                future.set_exception(e)
                cooldown = self._settings.bibigpt_failure_cooldown_seconds
                if cooldown > 0:
                    _remember(
                        self._summary_failures,
                        url,
                        (time.monotonic() + cooldown, _summary_failure_message(e)),
                    )
            else:  # cancellation: waiters get a normal failure, not our cancel
                future.set_exception(BibiAPIError(0, "summary generation was cancelled"))
            future.exception()  # mark retrieved so an unwaited future stays quiet on GC
            raise
        else:
            future.set_result(result)
            ttl = self._settings.bibigpt_summary_cache_ttl_seconds
            if ttl > 0:
                _remember(self._recent_summaries, url, (time.monotonic() + ttl, result))
            return result
        finally:
            self._inflight_summaries.pop(url, None)

    async def _lookup_cached_summary(
        self,
        url: str,
        prompt: str | None,
        message_id: str,
    ) -> SummaryResult | None:
        """Reuse an already-generated summary when this video demonstrably has
        one: a contentId remembered in-process or read back from the archived
        row's BibiGPT link. Any miss or lookup failure returns None and the
        caller regenerates; custom-prompt requests never take this path."""
        content_id = self._bibigpt_content_id.get(url) or ""
        if not content_id and self._archive is not None:
            try:
                content_id = await self._archive.find_bibigpt_content_id(
                    self._source_archive_url.get(url, url)
                )
            except Exception as e:
                logger.warning("bibigpt content-id lookup failed: url=%s error=%s", url, e)
                return None
        if not content_id:
            return None
        result = await self._bibi_client.summarize_cached(url, prompt=prompt)
        if result is not None:
            logger.info(
                "using cached BibiGPT summary: content_id=%s message_id=%s",
                result.content_id or content_id,
                message_id,
            )
        return result

    async def _try_send_bibigpt_chapter_summary(
        self,
        event: MessageEvent | CardActionEvent,
        result: SummaryResult,
    ) -> None:
        try:
            chapter_result = await self._bibi_client.fetch_chapter_summary(result)
        except Exception as e:
            logger.warning(
                "chapter summary lookup failed unexpectedly: content_id=%s message_id=%s error=%s",
                result.content_id,
                event.message_id,
                e,
            )
            await self._text_sender.send(
                "BibiGPT 字幕总结暂不可用: 没有获取到章节总结。",
                event.chat_id,
                event.message_id,
            )
            return

        sections = chapter_result.sections
        logger.info(
            "chapter summary lookup complete: content_id=%s source=%s status=%s "
            "sections=%d message_id=%s reason=%s",
            result.content_id,
            chapter_result.source,
            chapter_result.status,
            len(sections),
            event.message_id,
            chapter_result.reason or "(none)",
        )
        if not sections:
            await self._text_sender.send(
                "BibiGPT 字幕总结暂不可用: 没有获取到章节总结。",
                event.chat_id,
                event.message_id,
            )
            return

        try:
            introduction, formatted_sections = await self._translator.format_chapter_summary(
                chapter_result.introduction,
                sections,
                content_id=result.content_id,
            )
        except Exception as e:
            logger.warning(
                "chapter summary formatting failed unexpectedly, using BibiGPT text: "
                "content_id=%s sections=%d message_id=%s error=%s",
                result.content_id,
                len(sections),
                event.message_id,
                e,
            )
            introduction = chapter_result.introduction
            formatted_sections = sections

        try:
            cards = build_chapter_summary_cards(introduction, formatted_sections)
        except Exception as e:
            logger.error(
                "chapter summary card build failed: content_id=%s sections=%d "
                "message_id=%s error=%s",
                result.content_id,
                len(formatted_sections),
                event.message_id,
                e,
            )
            await self._text_sender.send(
                "BibiGPT 字幕总结暂不可用: 卡片生成失败。",
                event.chat_id,
                event.message_id,
            )
            return

        total = len(cards)
        logger.info(
            "chapter summary cards ready: content_id=%s sections=%d cards=%d message_id=%s",
            result.content_id,
            len(formatted_sections),
            total,
            event.message_id,
        )
        for part_index, card_json in enumerate(cards, start=1):
            if part_index > 1:
                await asyncio.sleep(_CHAPTER_SUMMARY_SEND_INTERVAL_SECONDS)
            wire_bytes = card_message_wire_size(card_json)
            card_sent = await self._sender.send(card_json, event.chat_id, event.message_id)
            if not card_sent:
                logger.error(
                    "chapter summary card send failed: content_id=%s part=%d/%d "
                    "wire_bytes=%d message_id=%s",
                    result.content_id,
                    part_index,
                    total,
                    wire_bytes,
                    event.message_id,
                )
                await self._text_sender.send(
                    f"BibiGPT 字幕总结发送不完整: 已发送 {part_index - 1}/{total} 段, 可重试。",
                    event.chat_id,
                    event.message_id,
                )
                return
            logger.info(
                "chapter summary card sent: content_id=%s part=%d/%d wire_bytes=%d message_id=%s",
                result.content_id,
                part_index,
                total,
                wire_bytes,
                event.message_id,
            )

    async def _try_send_comment_analysis(
        self,
        url: str,
        event: MessageEvent | CardActionEvent,
    ) -> None:
        async with self._typing_sender.hold(event.message_id, label="comments"):
            try:
                result = await self._comment_analyzer.analyze(url)
            except Exception as e:
                error = str(e) or e.__class__.__name__
                logger.error(
                    "comment analysis failed: url=%s message_id=%s error=%s",
                    url,
                    event.message_id,
                    error,
                )
                await self._text_sender.send(
                    _comment_analysis_failure_message(e),
                    event.chat_id,
                    event.message_id,
                )
                return

            try:
                card_json = build_markdown_card(
                    "评论区分析",
                    result.markdown,
                    source_url=url,
                    collapsed=True,
                    panel_title="评论区分析",
                    sectioned=True,
                )
                card_sent = await self._sender.send(card_json, event.chat_id, event.message_id)
                if not card_sent:
                    logger.error(
                        "comment analysis card send failed: url=%s message_id=%s",
                        url,
                        event.message_id,
                    )
                    return
                logger.info(
                    "comment analysis done: url=%s total_comments=%s comments=%d "
                    "prompt_comments=%d message_id=%s",
                    url,
                    result.total_comment_count
                    if result.total_comment_count is not None
                    else "unknown",
                    result.fetched_count,
                    result.prompt_count,
                    event.message_id,
                )
            except Exception as e:
                logger.error(
                    "comment analysis reply failed: url=%s message_id=%s error=%s",
                    url,
                    event.message_id,
                    e,
                )

    async def _fetch_bot_open_id(self) -> str:
        request = (
            lark.BaseRequest.builder()
            .http_method(lark.HttpMethod.GET)
            .uri("/open-apis/bot/v3/info")
            .token_types({lark.AccessTokenType.TENANT})
            .build()
        )
        try:
            resp = await self._lark_client.arequest(request)
            if not resp.success():
                logger.warning("failed to fetch bot info: code=%s msg=%s", resp.code, resp.msg)
                return ""
            import json

            data = json.loads(resp.raw.content.decode("utf-8"))
            return data.get("bot", {}).get("open_id", "")
        except Exception as e:
            logger.warning("failed to fetch bot info: %s", e)
            return ""


def _download_failure_message(url: str, reason: str) -> str:
    return f"下载失败: {_friendly_download_reason(reason)}\n{url}"


def _comment_analysis_failure_message(exc: Exception) -> str:
    if isinstance(exc, CommentAnalysisError):
        return f"评论区分析失败: {exc!s}"
    return f"评论区分析失败: {str(exc) or exc.__class__.__name__}"


def _friendly_download_reason(reason: str) -> str:
    if reason == "video append disabled":
        return "视频下载功能已关闭。"
    if reason.startswith("platform not allowed:"):
        platform = reason.split(":", 1)[1].strip()
        return f"该平台不在允许下载列表中: {platform}。"
    if reason.startswith("not a video:"):
        return "这个链接没有识别为可下载视频。"
    if reason == "video duration unknown":
        return "无法提前确认视频时长, 自动追加已跳过。手动发送 下载 <链接> 可强制尝试。"
    if reason.startswith("video duration exceeds limit:"):
        duration = _reason_value(reason, "duration")
        limit = _reason_value(reason, "limit")
        if duration and limit:
            return f"视频时长 {duration} 秒, 超过自动追加限制 {limit} 秒。"
        return "视频时长超过自动追加限制。"
    if reason == "platform requires auth":
        return "该平台需要登录态 cookie。"
    if reason == "no download candidate":
        return "没有找到可下载的视频文件。"
    if reason.startswith("video filesize exceeds limit:"):
        size_mb = _reason_value(reason, "size_mb")
        limit_mb = _reason_value(reason, "limit_mb")
        if size_mb and limit_mb:
            return f"视频候选文件约 {size_mb} MB, 超过当前限制 {limit_mb} MB。"
        return "视频候选文件超过当前体积限制。"
    if reason.startswith("downloaded video too large:"):
        size_mb = _reason_value(reason, "size_mb")
        limit_mb = _reason_value(reason, "limit_mb")
        if size_mb and limit_mb:
            return f"下载后文件约 {size_mb} MB, 超过当前限制 {limit_mb} MB。"
        return "下载后文件超过当前体积限制。"
    return reason


def _summary_failure_message(exc: Exception) -> str:
    if isinstance(exc, SummaryCooldownError):
        wait = exc.remaining_seconds
        wait_text = f"{wait} 秒" if wait < 120 else f"{round(wait / 60)} 分钟"
        reason = exc.reason.removeprefix("BibiGPT 总结失败: ")
        return (
            f"BibiGPT 总结失败: 该视频刚失败过, 已进入冷却, 请 {wait_text}后再试。"
            f"上次原因: {reason}"
        )

    if isinstance(exc, AuthenticationError):
        if exc.status_code == 0:
            return (
                "BibiGPT 总结失败: 本地 cookie 文件不完整或已损坏, "
                "请重新导出 aitodo.co cookies 后更新 cookies/bibigpt.txt。"
            )
        return "BibiGPT 总结失败: 登录态已失效, 需要在服务器上重新登录 aitodo.co 账号后重试。"

    if isinstance(exc, TranscriptUnavailableError):
        return (
            "BibiGPT 总结失败: 没有拿到这个视频的字幕或转录文本, "
            "可能是视频没有字幕、字幕被限制, 或转录抓取临时失败, 可稍后重试。"
        )

    if isinstance(exc, BibiAPIError):
        status = exc.status_code
        body = exc.body or ""
        if status == 402 or "PAYMENT_REQUIRED" in body or "余额不足" in body:
            return (
                "BibiGPT 总结失败: 账号额度不足或会员已过期, 需要为 aitodo.co 账号充值/续费后重试。"
            )
        if status == 429:
            return "BibiGPT 总结失败: 触发接口限流, 请稍后重试。"
        if "平台风控" in body:
            return (
                "BibiGPT 总结失败: BibiGPT 抓取 B 站字幕被风控拦截, "
                "已自动等待并重试三次仍失败, 请过几分钟再点一次“总结视频”。"
            )
        if "service returned an HTML error page" in str(exc):
            return (
                f"BibiGPT 总结失败: 服务返回了 HTML 错误页 (HTTP {status}), "
                "可能是登录态异常或服务端临时异常, 请稍后重试。"
            )
        if status in (500, 502, 503, 504) or status == 0 or "Connection error" in body:
            suffix = f" (HTTP {status})" if status else ""
            return (
                f"BibiGPT 总结失败: BibiGPT 服务端暂时不稳定{suffix}, 通常是临时故障, 请稍后重试。"
            )
        return f"BibiGPT 总结失败: {exc!s}"

    return f"BibiGPT 总结失败: {str(exc) or exc.__class__.__name__}"


def _reason_value(reason: str, key: str) -> str:
    match = re.search(rf"\b{re.escape(key)}=([0-9.]+)", reason)
    return match.group(1) if match else ""
