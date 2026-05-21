from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import lark_oapi as lark

from .config import Settings

logger = logging.getLogger(__name__)


@dataclass
class MessageEvent:
    sender_id: str
    message_id: str
    chat_id: str
    chat_type: str
    message_type: str
    content: str
    timestamp_utc: datetime
    mentions: list[str]


class LarkEventListener:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._queue: asyncio.Queue[MessageEvent] = asyncio.Queue()

    async def listen(self) -> AsyncIterator[MessageEvent]:
        loop = asyncio.get_running_loop()

        def on_message(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
            event = _parse_event(data)
            if event is not None:
                loop.call_soon_threadsafe(self._queue.put_nowait, event)

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(on_message)
            .build()
        )
        ws_client = lark.ws.Client(
            self._settings.app_id,
            self._settings.app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.WARNING,
        )
        loop.run_in_executor(None, ws_client.start)
        logger.info("WebSocket long connection started")

        while True:
            event = await self._queue.get()
            yield event


def _parse_event(data: lark.im.v1.P2ImMessageReceiveV1) -> MessageEvent | None:
    try:
        msg = data.event.message
        sender = data.event.sender
        mentions = getattr(msg, "mentions", None)
        mention_ids = []
        if mentions:
            for m in mentions:
                if m.id and m.id.open_id:
                    mention_ids.append(m.id.open_id)

        return MessageEvent(
            sender_id=sender.sender_id.open_id,
            message_id=msg.message_id,
            chat_id=msg.chat_id,
            chat_type=msg.chat_type,
            message_type=msg.message_type,
            content=msg.content,
            timestamp_utc=datetime.now(UTC),
            mentions=mention_ids,
        )
    except AttributeError as e:
        logger.warning("failed to parse event: %s", e)
        return None
