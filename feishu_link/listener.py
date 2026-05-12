from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

from .config import Settings

logger = logging.getLogger(__name__)

EVENT_KEY = "im.message.receive_v1"


@dataclass
class MessageEvent:
    sender_id: str
    message_id: str
    chat_id: str
    chat_type: str
    message_type: str
    content: str
    timestamp_utc: datetime


class LarkEventListener:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def listen(self) -> AsyncIterator[MessageEvent]:
        while True:
            try:
                async for event in self._consume():
                    yield event
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error("listener error, reconnecting in 5s: %s", e)
                await asyncio.sleep(5)

    async def _consume(self) -> AsyncIterator[MessageEvent]:
        cmd = [
            self._settings.lark_cli_bin,
            "event", "consume", EVENT_KEY,
            "--as", "user",
            "--format", "ndjson",
        ]
        logger.info("starting lark-cli event consumer: %s", " ".join(cmd))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
        )
        assert proc.stdout is not None
        assert proc.stderr is not None

        # Wait for ready marker on stderr
        await _wait_ready(proc.stderr)
        logger.info("lark-cli event consumer ready")

        try:
            async for line in proc.stdout:
                raw = line.strip()
                if not raw:
                    continue
                event = _parse_event(raw.decode(errors="replace"))
                if event is not None:
                    yield event
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except TimeoutError:
                    proc.kill()


async def _wait_ready(stderr: asyncio.StreamReader) -> None:
    async def _read() -> None:
        async for line in stderr:
            text = line.decode(errors="replace")
            logger.debug("lark-cli stderr: %s", text.rstrip())
            if "[event] ready" in text:
                return

    try:
        await asyncio.wait_for(_read(), timeout=30)
    except TimeoutError as e:
        raise RuntimeError("lark-cli did not emit ready signal within 30s") from e


def _parse_event(raw: str) -> MessageEvent | None:
    try:
        obj: dict = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("failed to parse event JSON: %r", raw[:200])
        return None

    try:
        return MessageEvent(
            sender_id=obj["sender_id"],
            message_id=obj["message_id"],
            chat_id=obj["chat_id"],
            chat_type=obj.get("chat_type", ""),
            message_type=obj.get("message_type", "text"),
            content=obj.get("content", ""),
            timestamp_utc=datetime.now(tz=UTC),
        )
    except KeyError as e:
        logger.warning("event missing required field %s: %r", e, raw[:200])
        return None
