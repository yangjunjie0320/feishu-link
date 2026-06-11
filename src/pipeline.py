from __future__ import annotations

import logging
import re
from collections import OrderedDict

import httpx
import lark_oapi as lark

from .bibi_client import (
    AuthenticationError,
    BibiAPIError,
    BibiClient,
    TranscriptUnavailableError,
)
from .card import build_card, build_markdown_card
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
from .url_extract import extract_prompt, extract_urls, is_manual_download_command

logger = logging.getLogger(__name__)

_SEEN_CAPACITY = 500


class Pipeline:
    def __init__(self, settings: Settings, lark_client: lark.Client) -> None:
        self._settings = settings
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
            logger.debug("skip: no URLs in message_id=%s", event.message_id)
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
                "summarizing video=%s prompt=%s message_id=%s",
                url,
                prompt[:30] if prompt else "(default)",
                event.message_id,
            )

            try:
                result = await self._bibi_client.summarize(url, prompt=prompt)
                summary_content = await self._translator.ensure_chinese_markdown_summary(
                    result.content,
                    source_url=url,
                )
            except Exception as e:
                error = str(e) or e.__class__.__name__
                logger.error(
                    "summarize failed: url=%s message_id=%s error=%s",
                    url,
                    event.message_id,
                    error,
                )
                await self._text_sender.send(
                    _summary_failure_message(e),
                    event.chat_id,
                    event.message_id,
                )
                return

            try:
                card_json = build_markdown_card(
                    "BibiGPT 总结",
                    summary_content,
                    source_url=url,
                    collapsed=True,
                    panel_title="总结正文",
                    sectioned=True,
                )
                card_sent = await self._sender.send(
                    card_json, event.chat_id, event.message_id
                )
                if not card_sent:
                    logger.error(
                        "summary card send failed: url=%s message_id=%s",
                        url,
                        event.message_id,
                    )
                    return
                logger.info(
                    "done: video=%s tokens=%d cached=%s message_id=%s",
                    url,
                    result.usage.total_tokens,
                    result.from_cache,
                    event.message_id,
                )
            except Exception as e:
                logger.error(
                    "reply failed: url=%s message_id=%s error=%s",
                    url,
                    event.message_id,
                    e,
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
                card_sent = await self._sender.send(
                    card_json, event.chat_id, event.message_id
                )
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
    if isinstance(exc, AuthenticationError):
        if exc.status_code == 0:
            return (
                "BibiGPT 总结失败: 本地 cookie 文件不完整或已损坏, "
                "请重新导出 aitodo.co cookies 后更新 cookies/bibigpt.txt。"
            )
        return "BibiGPT 总结失败: BibiGPT 登录态已失效, 请重新导出 aitodo.co cookies。"

    if isinstance(exc, BibiAPIError):
        if isinstance(exc, TranscriptUnavailableError):
            return (
                "BibiGPT 总结失败: BibiGPT 没有拿到这个视频的字幕或转录文本, "
                "可能是视频没有字幕、字幕被限制, 或 YouTube 转录抓取临时失败。"
            )
        detail = str(exc)
        if "service returned an HTML error page" in detail:
            return (
                f"BibiGPT 总结失败: 服务返回了 HTML 错误页 (HTTP {exc.status_code}), "
                "通常是登录态异常或 BibiGPT 服务端临时异常。"
            )
        return f"BibiGPT 总结失败: {detail}"

    return f"BibiGPT 总结失败: {str(exc) or exc.__class__.__name__}"


def _reason_value(reason: str, key: str) -> str:
    match = re.search(rf"\b{re.escape(key)}=([0-9.]+)", reason)
    return match.group(1) if match else ""
