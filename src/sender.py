from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import lark_oapi as lark
import tenacity
from lark_oapi.api.im.v1 import (
    CreateFileRequest,
    CreateFileRequestBody,
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    DeleteMessageReactionRequest,
    Emoji,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from .config import Mode, Settings

logger = logging.getLogger(__name__)


class SendError(Exception):
    pass


class TypingReactionSender:
    def __init__(self, client: lark.Client) -> None:
        self._client = client

    @asynccontextmanager
    async def hold(
        self,
        message_id: str,
        *,
        label: str,
    ) -> AsyncIterator[None]:
        reaction_id = await self.start(message_id, label=label)
        try:
            yield
        finally:
            await self.stop(message_id, reaction_id, label=label)

    async def start(self, message_id: str, *, label: str = "work") -> str | None:
        request = (
            CreateMessageReactionRequest.builder()
            .message_id(message_id)
            .request_body(
                CreateMessageReactionRequestBody.builder()
                .reaction_type(Emoji.builder().emoji_type("Typing").build())
                .build()
            )
            .build()
        )

        try:
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None, lambda: self._client.im.v1.message_reaction.create(request)
            )
        except Exception as e:
            logger.warning(
                "typing reaction add failed: label=%s message_id=%s error=%s",
                label,
                message_id,
                e,
            )
            return None

        if not response.success():
            logger.warning(
                "typing reaction add failed: label=%s message_id=%s code=%s msg=%s",
                label,
                message_id,
                response.code,
                response.msg,
            )
            return None

        reaction_id = getattr(response.data, "reaction_id", None)
        if not reaction_id:
            logger.warning(
                "typing reaction add returned no reaction_id: label=%s message_id=%s",
                label,
                message_id,
            )
            return None

        logger.info(
            "typing reaction added: label=%s message_id=%s reaction_id=%s",
            label,
            message_id,
            reaction_id,
        )
        return reaction_id

    async def stop(
        self,
        message_id: str,
        reaction_id: str | None,
        *,
        label: str = "work",
    ) -> bool:
        if not reaction_id:
            return True

        request = (
            DeleteMessageReactionRequest.builder()
            .message_id(message_id)
            .reaction_id(reaction_id)
            .build()
        )

        try:
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None, lambda: self._client.im.v1.message_reaction.delete(request)
            )
        except Exception as e:
            logger.warning(
                "typing reaction remove failed: label=%s message_id=%s reaction_id=%s error=%s",
                label,
                message_id,
                reaction_id,
                e,
            )
            return False

        if not response.success():
            logger.warning(
                "typing reaction remove failed: label=%s message_id=%s reaction_id=%s "
                "code=%s msg=%s",
                label,
                message_id,
                reaction_id,
                response.code,
                response.msg,
            )
            return False

        logger.info(
            "typing reaction removed: label=%s message_id=%s reaction_id=%s",
            label,
            message_id,
            reaction_id,
        )
        return True


class CardSender:
    def __init__(self, settings: Settings, client: lark.Client) -> None:
        self._settings = settings
        self._client = client

    async def send(self, card_json: str, chat_id: str, message_id: str) -> bool:
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
            return False
        return True

    async def _reply(self, card_json: str, message_id: str) -> None:
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("interactive")
                .content(card_json)
                .build()
            )
            .build()
        )
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None, lambda: self._client.im.v1.message.reply(request)
        )
        if not response.success():
            raise SendError(f"reply failed: code={response.code} msg={response.msg}")
        logger.info("card sent as thread reply: message_id=%s", message_id)

    async def _send_to_archive(self, card_json: str) -> None:
        if not self._settings.archive_chat_id:
            raise SendError("archive_chat_id not configured for mode B")
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(self._settings.archive_chat_id)
                .msg_type("interactive")
                .content(card_json)
                .build()
            )
            .build()
        )
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None, lambda: self._client.im.v1.message.create(request)
        )
        if not response.success():
            raise SendError(f"create failed: code={response.code} msg={response.msg}")
        logger.info("card sent to archive: chat_id=%s", self._settings.archive_chat_id)


class VideoSender:
    def __init__(self, settings: Settings, client: lark.Client) -> None:
        self._settings = settings
        self._client = client

    async def send(
        self,
        path: Path,
        file_name: str,
        duration_ms: int,
        chat_id: str,
        message_id: str,
        image_key: str | None = None,
    ) -> None:
        @tenacity.retry(
            stop=tenacity.stop_after_attempt(self._settings.send_retry_attempts),
            wait=tenacity.wait_exponential(multiplier=1, min=1, max=9),
            reraise=True,
        )
        async def _attempt() -> None:
            file_key = await self._upload(path, file_name, duration_ms)
            content = build_media_content(file_key, image_key)
            if self._settings.mode == Mode.A:
                await self._reply_media(content, message_id)
            else:
                await self._send_media_to_archive(content)

        await _attempt()
        logger.info(
            "video sent: file_name=%s chat_id=%s message_id=%s",
            file_name,
            chat_id,
            message_id,
        )

    async def _upload(self, path: Path, file_name: str, duration_ms: int) -> str:
        def _upload_sync() -> str:
            try:
                with path.open("rb") as f:
                    request = (
                        CreateFileRequest.builder()
                        .request_body(
                            CreateFileRequestBody.builder()
                            .file_type("mp4")
                            .file_name(file_name)
                            .duration(duration_ms)
                            .file(f)
                            .build()
                        )
                        .build()
                    )
                    response = self._client.im.v1.file.create(request)
            except Exception as e:
                raise SendError(f"video upload failed: {e}") from e

            if not response.success():
                raise SendError(
                    f"video upload failed: code={response.code} msg={response.msg}"
                )
            return response.data.file_key

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _upload_sync)

    async def _reply_media(self, content: str, message_id: str) -> None:
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("media")
                .content(content)
                .build()
            )
            .build()
        )
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None, lambda: self._client.im.v1.message.reply(request)
        )
        if not response.success():
            raise SendError(f"video reply failed: code={response.code} msg={response.msg}")

    async def _send_media_to_archive(self, content: str) -> None:
        if not self._settings.archive_chat_id:
            raise SendError("archive_chat_id not configured for mode B")
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(self._settings.archive_chat_id)
                .msg_type("media")
                .content(content)
                .build()
            )
            .build()
        )
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None, lambda: self._client.im.v1.message.create(request)
        )
        if not response.success():
            raise SendError(f"video create failed: code={response.code} msg={response.msg}")


def build_media_content(file_key: str, image_key: str | None = None) -> str:
    content = {"file_key": file_key}
    if image_key:
        content["image_key"] = image_key
    return json.dumps(content, ensure_ascii=False)


class TextSender:
    def __init__(self, settings: Settings, client: lark.Client) -> None:
        self._settings = settings
        self._client = client

    async def send(self, text: str, chat_id: str, message_id: str) -> bool:
        @tenacity.retry(
            stop=tenacity.stop_after_attempt(self._settings.send_retry_attempts),
            wait=tenacity.wait_exponential(multiplier=1, min=1, max=9),
            reraise=True,
        )
        async def _attempt() -> None:
            content_json = json.dumps({"text": text}, ensure_ascii=False)
            if self._settings.mode == Mode.A:
                await self._reply_text(content_json, message_id)
            else:
                await self._send_text_to_archive(content_json)

        try:
            await _attempt()
        except Exception as e:
            logger.critical(
                "text send exhausted all retries: message_id=%s error=%s",
                message_id,
                e,
            )
            return False
        return True

    async def _reply_text(self, content: str, message_id: str) -> None:
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("text")
                .content(content)
                .build()
            )
            .build()
        )
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None, lambda: self._client.im.v1.message.reply(request)
        )
        if not response.success():
            raise SendError(f"text reply failed: code={response.code} msg={response.msg}")
        logger.info("text sent as thread reply: message_id=%s", message_id)

    async def _send_text_to_archive(self, content: str) -> None:
        if not self._settings.archive_chat_id:
            raise SendError("archive_chat_id not configured for mode B")
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(self._settings.archive_chat_id)
                .msg_type("text")
                .content(content)
                .build()
            )
            .build()
        )
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None, lambda: self._client.im.v1.message.create(request)
        )
        if not response.success():
            raise SendError(f"text create failed: code={response.code} msg={response.msg}")
        logger.info("text sent to archive: chat_id=%s", self._settings.archive_chat_id)
