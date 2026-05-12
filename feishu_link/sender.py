from __future__ import annotations

import asyncio
import logging

import tenacity

from .config import Mode, Settings

logger = logging.getLogger(__name__)


class SendError(Exception):
    pass


class CardSender:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def send(self, card_json: str, chat_id: str, message_id: str) -> None:
        @tenacity.retry(
            stop=tenacity.stop_after_attempt(self._settings.send_retry_attempts),
            wait=tenacity.wait_exponential(multiplier=1, min=1, max=9),
            reraise=True,
        )
        async def _attempt() -> None:
            if self._settings.mode == Mode.A:
                await self._reply(card_json, message_id)
            else:
                await self._send_to_archive(card_json)

        try:
            await _attempt()
        except Exception as e:
            logger.critical(
                "card send exhausted all retries: message_id=%s error=%s",
                message_id,
                e,
            )

    async def _reply(self, card_json: str, message_id: str) -> None:
        cmd = [
            self._settings.lark_cli_bin,
            "im", "+messages-reply",
            "--message-id", message_id,
            "--msg-type", "interactive",
            "--content", card_json,
            "--reply-in-thread",
            "--as", "user",
        ]
        await _run(cmd)
        logger.info("card sent as thread reply: message_id=%s", message_id)

    async def _send_to_archive(self, card_json: str) -> None:
        if not self._settings.archive_chat_id:
            raise SendError("archive_chat_id not configured for mode B")
        cmd = [
            self._settings.lark_cli_bin,
            "im", "+messages-send",
            "--chat-id", self._settings.archive_chat_id,
            "--msg-type", "interactive",
            "--content", card_json,
            "--as", "user",
        ]
        await _run(cmd)
        logger.info("card sent to archive: chat_id=%s", self._settings.archive_chat_id)


async def _run(cmd: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise SendError(
            f"lark-cli exited {proc.returncode}: {stderr.decode(errors='replace')}"
        )
    logger.debug("lark-cli output: %s", stdout.decode(errors="replace"))
