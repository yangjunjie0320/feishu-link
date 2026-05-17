from __future__ import annotations

import logging
from collections import OrderedDict

import httpx
import lark_oapi as lark

from .card import build_card
from .config import Settings
from .dispatch import Dispatcher
from .image_uploader import upload_cover
from .listener import MessageEvent
from .media_downloader import VideoSkipReason, download_video
from .parsers.base import LinkMetadata, ParserError
from .sender import CardSender, VideoSender
from .translator import TitleTranslator
from .url_extract import extract_urls

logger = logging.getLogger(__name__)

_SEEN_CAPACITY = 500


class Pipeline:
    def __init__(self, settings: Settings, lark_client: lark.Client) -> None:
        self._settings = settings
        self._http = httpx.AsyncClient(timeout=settings.request_timeout)
        self._dispatcher = Dispatcher(settings, self._http)
        self._translator = TitleTranslator(settings, self._http)
        self._sender = CardSender(settings, lark_client)
        self._video_sender = VideoSender(settings, lark_client)
        self._lark_client = lark_client
        self._seen: OrderedDict[str, None] = OrderedDict()

    async def handle(self, event: MessageEvent) -> None:
        if event.message_id in self._seen:
            logger.debug("skip duplicate message_id=%s", event.message_id)
            return
        self._seen[event.message_id] = None
        if len(self._seen) > _SEEN_CAPACITY:
            self._seen.popitem(last=False)

        urls = extract_urls(event.message_type, event.content, self._settings)
        if not urls:
            logger.debug("skip: no URLs in message_id=%s", event.message_id)
            return

        logger.info(
            "processing %d URL(s) from message_id=%s", len(urls), event.message_id
        )

        for url in urls:
            await self._process_url(url, event)

    async def _process_url(self, url: str, event: MessageEvent) -> None:
        logger.debug("parsing url=%s", url)
        try:
            meta = await self._dispatcher.parse(url)
        except ParserError as e:
            logger.error(
                "parse failed: url=%s message_id=%s reason=%s",
                url,
                event.message_id,
                e.reason,
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
        await self._try_send_video(meta, event, img_key)

    async def _try_send_video(
        self,
        meta: LinkMetadata,
        event: MessageEvent,
        img_key: str | None,
    ) -> None:
        try:
            video = await download_video(meta, self._settings)
        except VideoSkipReason as e:
            logger.info(
                "skip video append: url=%s message_id=%s reason=%s",
                meta.source_url,
                event.message_id,
                e,
            )
            return
        except Exception as e:
            logger.warning(
                "video download failed: url=%s message_id=%s error=%s",
                meta.source_url,
                event.message_id,
                e,
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
        finally:
            video.cleanup()
