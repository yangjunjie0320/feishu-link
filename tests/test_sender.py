import asyncio
import json
from json import JSONDecodeError
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from src.config import Settings
from src.sender import CardSender, TypingReactionSender, VideoSender, build_media_content


class _FakeResponse:
    def __init__(self, success: bool = True, data: object | None = None) -> None:
        self._success = success
        self.data = data
        self.code = 0 if success else 999
        self.msg = "ok" if success else "failed"

    def success(self) -> bool:
        return self._success


class _FakeMessageReaction:
    def __init__(self) -> None:
        self.create_requests = []
        self.delete_requests = []
        self._active_reactions: dict[tuple[str, str], str] = {}

    def create(self, request):
        self.create_requests.append(request)
        reaction_id = self._active_reactions.setdefault(
            (request.paths["message_id"], request.body.reaction_type.emoji_type),
            f"reaction_{len(self.create_requests)}",
        )
        return _FakeResponse(data=SimpleNamespace(reaction_id=reaction_id))

    def delete(self, request):
        self.delete_requests.append(request)
        for key, reaction_id in tuple(self._active_reactions.items()):
            if (
                key[0] == request.paths["message_id"]
                and reaction_id == request.paths["reaction_id"]
            ):
                del self._active_reactions[key]
        return _FakeResponse()


class _FakeClient:
    def __init__(self) -> None:
        self.message_reaction = _FakeMessageReaction()
        self.im = SimpleNamespace(
            v1=SimpleNamespace(message_reaction=self.message_reaction)
        )


class _BrokenFileService:
    def create(self, request):
        raise JSONDecodeError("Expecting value", "", 0)


class _FakeUploadClient:
    config = SimpleNamespace(domain="https://open.feishu.cn")

    def __init__(self) -> None:
        self.im = SimpleNamespace(v1=SimpleNamespace(file=_BrokenFileService()))


def test_build_media_content_without_cover() -> None:
    assert json.loads(build_media_content("file_xxx")) == {"file_key": "file_xxx"}


def test_build_media_content_with_cover() -> None:
    assert json.loads(build_media_content("file_xxx", "img_xxx")) == {
        "file_key": "file_xxx",
        "image_key": "img_xxx",
    }


async def test_typing_reaction_sender_adds_and_removes_typing_reaction() -> None:
    client = _FakeClient()
    sender = TypingReactionSender(client)

    reaction_id = await sender.start("om_message")
    stopped = await sender.stop("om_message", reaction_id)

    assert reaction_id == "reaction_1"
    assert stopped is True

    create_request = client.message_reaction.create_requests[0]
    assert create_request.paths["message_id"] == "om_message"
    assert create_request.body.reaction_type.emoji_type == "Typing"

    delete_request = client.message_reaction.delete_requests[0]
    assert delete_request.paths["message_id"] == "om_message"
    assert delete_request.paths["reaction_id"] == "reaction_1"


async def test_typing_reaction_sender_stop_ignores_missing_reaction() -> None:
    sender = TypingReactionSender(_FakeClient())

    assert await sender.stop("om_message", None) is True


async def test_typing_reaction_sender_hold_removes_current_reaction_on_error() -> None:
    client = _FakeClient()
    sender = TypingReactionSender(client)

    try:
        async with sender.hold("om_message", label="bibigpt"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    assert len(client.message_reaction.create_requests) == 1
    assert len(client.message_reaction.delete_requests) == 1
    delete_request = client.message_reaction.delete_requests[0]
    assert delete_request.paths["message_id"] == "om_message"
    assert delete_request.paths["reaction_id"] == "reaction_1"


async def test_typing_hold_shares_one_reaction_across_operation_labels() -> None:
    client = _FakeClient()
    sender = TypingReactionSender(client)

    async with sender.hold("om_message", label="card"):
        async with sender.hold("om_message", label="download"):
            assert len(client.message_reaction.create_requests) == 1
            request = client.message_reaction.create_requests[0]
            assert request.body.reaction_type.emoji_type == "Typing"
        assert client.message_reaction.delete_requests == []

    assert len(client.message_reaction.delete_requests) == 1
    assert client.message_reaction.delete_requests[0].paths["reaction_id"] == "reaction_1"


async def test_summary_and_comments_have_separate_reactions_on_the_same_message() -> None:
    client = _FakeClient()
    sender = TypingReactionSender(client)

    async with sender.hold("om_message", label="bibigpt"):
        async with sender.hold("om_message", label="comments"):
            emoji_types = [
                req.body.reaction_type.emoji_type
                for req in client.message_reaction.create_requests
            ]
            assert emoji_types == ["Typing", "THINKING"]
            assert client.message_reaction._active_reactions == {
                ("om_message", "Typing"): "reaction_1",
                ("om_message", "THINKING"): "reaction_2",
            }
        assert client.message_reaction._active_reactions == {
            ("om_message", "Typing"): "reaction_1"
        }
        assert client.message_reaction.delete_requests[0].paths["reaction_id"] == "reaction_2"

    assert [req.paths["reaction_id"] for req in client.message_reaction.delete_requests] == [
        "reaction_2", "reaction_1"
    ]
    assert client.message_reaction._active_reactions == {}


async def test_typing_holds_for_different_messages_have_independent_lifetimes() -> None:
    client = _FakeClient()
    sender = TypingReactionSender(client)

    async with sender.hold("om_summary", label="bibigpt"):
        async with sender.hold("om_comments", label="bibigpt"):
            assert len(client.message_reaction.create_requests) == 2
        assert [req.paths["message_id"] for req in client.message_reaction.delete_requests] == [
            "om_comments"
        ]

    assert [req.paths["message_id"] for req in client.message_reaction.delete_requests] == [
        "om_comments", "om_summary"
    ]


@pytest.mark.parametrize(
    ("outer_label", "inner_label", "shared"),
    [("bibigpt", "comments", False), ("comments", "bibigpt", False), ("card", "download", True)],
)
async def test_cancelling_one_operation_preserves_another_operations_reaction(
    outer_label: str, inner_label: str, shared: bool,
) -> None:
    client = _FakeClient()
    sender = TypingReactionSender(client)
    entered = asyncio.Event()

    async def inner_operation() -> None:
        async with sender.hold("om_message", label=inner_label):
            entered.set()
            await asyncio.Event().wait()

    async with sender.hold("om_message", label=outer_label):
        task = asyncio.create_task(inner_operation())
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(client.message_reaction.delete_requests) == (0 if shared else 1)
        assert list(client.message_reaction._active_reactions.values()) == ["reaction_1"]

    expected_count = 1 if shared else 2
    assert len(client.message_reaction.create_requests) == expected_count
    assert len(client.message_reaction.delete_requests) == expected_count
    assert client.message_reaction._active_reactions == {}


@pytest.mark.parametrize("another_waiter", [False, True])
async def test_cancelling_during_reaction_creation_does_not_abandon_the_reaction(
    another_waiter: bool,
) -> None:
    sender = TypingReactionSender(_FakeClient())
    creating = asyncio.Event()
    created = asyncio.Event()
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def start(message_id: str, *, label: str) -> str:
        creating.set()
        await created.wait()
        return "reaction_shared"

    sender.start = AsyncMock(side_effect=start)  # type: ignore[method-assign]
    sender.stop = AsyncMock(return_value=True)  # type: ignore[method-assign]

    async def operation(label: str) -> None:
        async with sender.hold("om_message", label=label):
            entered.set()
            await finish.wait()

    first = asyncio.create_task(operation("card"))
    await asyncio.wait_for(creating.wait(), timeout=1)
    waiter = asyncio.create_task(operation("download")) if another_waiter else None
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.sleep(0)
    sender.stop.assert_not_awaited()
    created.set()
    with pytest.raises(asyncio.CancelledError):
        await first

    if waiter is not None:
        await asyncio.wait_for(entered.wait(), timeout=1)
        sender.stop.assert_not_awaited()
        finish.set()
        await waiter
    else:
        assert not entered.is_set()

    sender.start.assert_awaited_once()
    sender.stop.assert_awaited_once()
    assert sender.stop.await_args.args == ("om_message", "reaction_shared")


async def test_new_typing_hold_waits_for_previous_cleanup_even_if_owner_is_cancelled() -> None:
    sender = TypingReactionSender(_FakeClient())
    removing = asyncio.Event()
    removed = asyncio.Event()
    new_entered = asyncio.Event()
    finish = asyncio.Event()
    calls: list[tuple[str, str]] = []

    async def start(message_id: str, *, label: str) -> str:
        calls.append(("start", label))
        return "reaction_" + label

    async def stop(message_id: str, reaction_id: str | None, *, label: str) -> bool:
        if label == "card" and not removing.is_set():
            removing.set()
            await removed.wait()
        calls.append(("stop", label))
        return True

    sender.start = AsyncMock(side_effect=start)  # type: ignore[method-assign]
    sender.stop = AsyncMock(side_effect=stop)  # type: ignore[method-assign]

    async def first_operation() -> None:
        async with sender.hold("om_message", label="card"):
            pass

    async def next_operation() -> None:
        async with sender.hold("om_message", label="download"):
            new_entered.set()
            await finish.wait()

    first = asyncio.create_task(first_operation())
    await asyncio.wait_for(removing.wait(), timeout=1)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(next_operation())
    async with sender.hold("om_message", label="comments"):
        assert not new_entered.is_set()
        assert calls == [("start", "card"), ("start", "comments")]
    removed.set()
    await asyncio.wait_for(new_entered.wait(), timeout=1)
    assert calls[-2:] == [("stop", "card"), ("start", "download")]
    finish.set()
    await second

    assert calls[-1] == ("stop", "download")


@respx.mock
async def test_video_upload_falls_back_to_raw_http_when_sdk_parse_fails(tmp_path) -> None:
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"fake mp4")
    respx.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"code": 0, "msg": "ok", "tenant_access_token": "tenant-token"},
        )
    )
    upload_route = respx.post("https://open.feishu.cn/open-apis/im/v1/files").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "success",
                "data": {"file_key": "file_v3_xxx"},
            },
        )
    )
    sender = VideoSender(
        Settings(app_id="cli_xxx", app_secret="secret", request_timeout=1),
        _FakeUploadClient(),
    )

    file_key = await sender._upload(video_path, "video.mp4", 1234)

    assert file_key == "file_v3_xxx"
    assert upload_route.called
    assert upload_route.calls.last.request.headers["Authorization"] == (
        "Bearer tenant-token"
    )


class _FakeMessageService:
    def __init__(self, fail_times: int = 0) -> None:
        self.create_requests = []
        self._fail_times = fail_times

    def create(self, request):
        self.create_requests.append(request)
        if len(self.create_requests) <= self._fail_times:
            return _FakeResponse(success=False)
        return _FakeResponse()


def _card_sender_with(service: _FakeMessageService, **settings_kwargs):
    client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=service)))
    return CardSender(Settings(**settings_kwargs), client)


async def test_send_to_chat_targets_given_chat() -> None:
    service = _FakeMessageService()
    sender = _card_sender_with(service)

    assert await sender.send_to_chat('{"card": 1}', "oc_target") is True
    assert len(service.create_requests) == 1
    body = service.create_requests[0].request_body
    assert body.receive_id == "oc_target"
    assert body.msg_type == "interactive"


async def test_send_to_chat_returns_false_after_retries_exhausted() -> None:
    service = _FakeMessageService(fail_times=10)
    sender = _card_sender_with(service, send_retry_attempts=2)

    assert await sender.send_to_chat('{"card": 1}', "oc_target") is False
    assert len(service.create_requests) == 2
