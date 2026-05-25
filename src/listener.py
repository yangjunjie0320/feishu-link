from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import lark_oapi as lark
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

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


@dataclass
class CardActionEvent:
    action: str
    source_url: str
    message_id: str
    chat_id: str
    operator_open_id: str


ListenerEvent = MessageEvent | CardActionEvent


class LarkEventListener:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._queue: asyncio.Queue[ListenerEvent] = asyncio.Queue()

    async def listen(self) -> AsyncIterator[ListenerEvent]:
        loop = asyncio.get_running_loop()

        def on_message(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
            event = _parse_event(data)
            if event is not None:
                loop.call_soon_threadsafe(self._queue.put_nowait, event)

        def on_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
            logger.info("card action callback received")
            event = _parse_card_action_event(data)
            if event is not None:
                logger.info(
                    "card action callback accepted: action=%s message_id=%s",
                    event.action,
                    event.message_id,
                )
                loop.call_soon_threadsafe(self._queue.put_nowait, event)
                return P2CardActionTriggerResponse({
                    "toast": {"type": "info", "content": "已开始处理"},
                })
            logger.warning("card action callback ignored: unrecognized payload")
            return P2CardActionTriggerResponse({
                "toast": {"type": "warning", "content": "无法识别这个按钮动作"},
            })

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(on_message)
            .register_p2_card_action_trigger(on_card_action)
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


def _parse_card_action_event(data: P2CardActionTrigger) -> CardActionEvent | None:
    try:
        event = data.event
        if event is None or event.action is None:
            return None

        value = _coerce_action_value(event.action.value)
        action = str(value.get("action") or "").strip()
        source_url = str(value.get("url") or value.get("source_url") or "").strip()
        if not action or not source_url:
            return None

        context = event.context
        message_id = str(getattr(context, "open_message_id", "") or "")
        chat_id = str(getattr(context, "open_chat_id", "") or "")
        if not message_id or not chat_id:
            logger.warning(
                "card action missing context: action=%s url=%s message_id=%s chat_id=%s",
                action,
                source_url,
                message_id,
                chat_id,
            )
            return None

        operator = event.operator
        return CardActionEvent(
            action=action,
            source_url=source_url,
            message_id=message_id,
            chat_id=chat_id,
            operator_open_id=str(getattr(operator, "open_id", "") or ""),
        )
    except AttributeError as e:
        logger.warning("failed to parse card action event: %s", e)
        return None


def _coerce_action_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}
