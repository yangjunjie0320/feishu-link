from __future__ import annotations

import logging

import httpx

from .card import build_card
from .config import Settings
from .dispatch import Dispatcher
from .listener import MessageEvent
from .parsers.base import ParserError
from .sender import CardSender
from .url_extract import extract_urls

logger = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        client = httpx.AsyncClient(timeout=settings.request_timeout)
        self._dispatcher = Dispatcher(settings, client)
        self._sender = CardSender(settings)

    async def handle(self, event: MessageEvent) -> None:
        if event.sender_id != self._settings.self_open_id:
            logger.debug("skip: not self message sender_id=%s", event.sender_id)
            return

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

        card_json = build_card(meta)
        await self._sender.send(card_json, event.chat_id, event.message_id)
        logger.info(
            "card sent: url=%s title=%r message_id=%s",
            url,
            meta.title[:50],
            event.message_id,
        )
