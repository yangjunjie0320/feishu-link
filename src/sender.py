from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
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

# Feishu reaction identifiers; text/card output keeps its existing formatting.
_WORK_REACTION_TYPES = {"bibigpt": "Typing", "comments": "THINKING"}


def _work_reaction_type(label: str) -> str:
    return _WORK_REACTION_TYPES.get(label, "Typing")


class SendError(Exception):
    pass


@dataclass
class _TypingHold:
    start_task: asyncio.Task[str | None]
    users: int = 0
    cleanup_task: asyncio.Task[None] | None = None


class TypingReactionSender:
    def __init__(self, client: lark.Client) -> None:
        self._client = client
        self._holds: dict[tuple[str, str], _TypingHold] = {}

    @asynccontextmanager
    async def hold(
        self,
        message_id: str,
        *,
        label: str,
    ) -> AsyncIterator[None]:
        # Different work reactions finish independently. Operations using the
        # same emoji still share Feishu's one bot/message/emoji reaction.
        key = (message_id, _work_reaction_type(label))
        while (state := self._holds.get(key)) is not None and state.cleanup_task:
            await asyncio.shield(state.cleanup_task)
        if state is None:
            state = _TypingHold(asyncio.create_task(self.start(message_id, label=label)))
            self._holds[key] = state
        state.users += 1
        try:
            await asyncio.shield(state.start_task)
            yield
        finally:
            state.users -= 1
            if state.users == 0:
                state.cleanup_task = asyncio.create_task(
                    self._clear_hold(key, state, label=label)
                )
                await asyncio.shield(state.cleanup_task)

    async def _clear_hold(
        self, key: tuple[str, str], state: _TypingHold, *, label: str
    ) -> None:
        try:
            # Even a cancelled initializer may already have sent its SDK call.
            # Wait for its reaction id so that it cannot leave an orphan behind.
            reaction_id = await state.start_task
            await self.stop(key[0], reaction_id, label=label)
        finally:
            self._holds.pop(key, None)

    async def start(self, message_id: str, *, label: str = "work") -> str | None:
        request = (
            CreateMessageReactionRequest.builder()
            .message_id(message_id)
            .request_body(
                CreateMessageReactionRequestBody.builder()
                .reaction_type(Emoji.builder().emoji_type(_work_reaction_type(label)).build())
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
                ReplyMessageRequestBody.builder().msg_type("interactive").content(card_json).build()
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
        await self._create_to_chat(card_json, self._settings.archive_chat_id)

    async def send_to_chat(self, card_json: str, chat_id: str) -> bool:
        """Send an interactive card to an arbitrary chat, with the same retry
        policy as send(). Used by flows that target a specific chat regardless
        of mode (e.g. the daily report)."""

        @tenacity.retry(
            stop=tenacity.stop_after_attempt(self._settings.send_retry_attempts),
            wait=tenacity.wait_exponential(multiplier=1, min=1, max=9),
            reraise=True,
        )
        async def _attempt() -> None:
            await self._create_to_chat(card_json, chat_id)

        try:
            await _attempt()
        except Exception as e:
            logger.critical(
                "card send to chat exhausted all retries: chat_id=%s error=%s",
                chat_id,
                e,
            )
            return False
        return True

    async def _create_to_chat(self, card_json: str, chat_id: str) -> None:
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
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
        logger.info("card sent to chat: chat_id=%s", chat_id)


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
            size_mb = path.stat().st_size / (1024 * 1024)
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
                logger.warning(
                    "video upload via SDK failed, trying raw HTTP fallback: "
                    "file_name=%s size_mb=%.2f duration_ms=%d error_type=%s error=%s",
                    file_name,
                    size_mb,
                    duration_ms,
                    e.__class__.__name__,
                    e,
                )
                return self._upload_raw_sync(path, file_name, duration_ms)

            if not response.success():
                raise SendError(f"video upload failed: code={response.code} msg={response.msg}")
            file_key = getattr(response.data, "file_key", "")
            if not file_key:
                raise SendError("video upload failed: response missing file_key")
            logger.info(
                "video upload via SDK succeeded: file_name=%s size_mb=%.2f duration_ms=%d",
                file_name,
                size_mb,
                duration_ms,
            )
            return file_key

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _upload_sync)

    def _upload_raw_sync(self, path: Path, file_name: str, duration_ms: int) -> str:
        domain = _lark_domain(self._client)
        timeout = httpx.Timeout(max(180.0, float(self._settings.request_timeout)), connect=10.0)
        size_mb = path.stat().st_size / (1024 * 1024)

        with httpx.Client(timeout=timeout) as client:
            token_response = client.post(
                f"{domain}/open-apis/auth/v3/tenant_access_token/internal",
                json={
                    "app_id": self._settings.app_id,
                    "app_secret": self._settings.app_secret,
                },
            )
            token_data = _response_json(
                token_response,
                context="tenant_access_token",
            )
            if token_response.status_code >= 400 or token_data.get("code") != 0:
                raise SendError(
                    "video upload token request failed: "
                    f"status={token_response.status_code} "
                    f"code={token_data.get('code')} msg={token_data.get('msg')} "
                    f"body={_response_body_prefix(token_response)}"
                )

            token = str(token_data.get("tenant_access_token") or "")
            if not token:
                raise SendError("video upload token request failed: missing token")

            with path.open("rb") as f:
                upload_response = client.post(
                    f"{domain}/open-apis/im/v1/files",
                    headers={"Authorization": f"Bearer {token}"},
                    data={
                        "file_type": "mp4",
                        "file_name": file_name,
                        "duration": str(duration_ms),
                    },
                    files={"file": (file_name, f, "video/mp4")},
                )

            upload_data = _response_json(upload_response, context="video upload")
            if upload_response.status_code >= 400 or upload_data.get("code") != 0:
                raise SendError(
                    "video upload failed: "
                    f"status={upload_response.status_code} "
                    f"content_type={upload_response.headers.get('content-type', '')} "
                    f"code={upload_data.get('code')} msg={upload_data.get('msg')} "
                    f"body={_response_body_prefix(upload_response)}"
                )

            data = upload_data.get("data")
            file_key = str(data.get("file_key") or "") if isinstance(data, dict) else ""
            if not file_key:
                raise SendError(
                    "video upload failed: missing file_key "
                    f"body={_response_body_prefix(upload_response)}"
                )

        logger.info(
            "video upload via raw HTTP succeeded: file_name=%s size_mb=%.2f duration_ms=%d",
            file_name,
            size_mb,
            duration_ms,
        )
        return file_key

    async def _reply_media(self, content: str, message_id: str) -> None:
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder().msg_type("media").content(content).build()
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


def _lark_domain(client: lark.Client) -> str:
    config = getattr(client, "config", None)
    domain = str(getattr(config, "domain", "") or "https://open.feishu.cn")
    return domain.rstrip("/")


def _response_json(response: httpx.Response, *, context: str) -> dict[str, object]:
    try:
        data = response.json()
    except ValueError as e:
        raise SendError(
            f"{context} returned non-json response: "
            f"status={response.status_code} "
            f"content_type={response.headers.get('content-type', '')} "
            f"body={_response_body_prefix(response)}"
        ) from e
    if not isinstance(data, dict):
        raise SendError(
            f"{context} returned invalid response shape: "
            f"status={response.status_code} body={_response_body_prefix(response)}"
        )
    return data


def _response_body_prefix(response: httpx.Response, *, limit: int = 300) -> str:
    return " ".join(response.text[:limit].split())


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
                ReplyMessageRequestBody.builder().msg_type("text").content(content).build()
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
