import json
from types import SimpleNamespace

from src.sender import TypingReactionSender, build_media_content


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

    def create(self, request):
        self.create_requests.append(request)
        return _FakeResponse(
            data=SimpleNamespace(reaction_id=f"reaction_{len(self.create_requests)}")
        )

    def delete(self, request):
        self.delete_requests.append(request)
        return _FakeResponse()


class _FakeClient:
    def __init__(self) -> None:
        self.message_reaction = _FakeMessageReaction()
        self.im = SimpleNamespace(
            v1=SimpleNamespace(message_reaction=self.message_reaction)
        )


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


async def test_typing_reaction_sender_uses_independent_reactions() -> None:
    client = _FakeClient()
    sender = TypingReactionSender(client)

    first = await sender.start("om_message", label="card")
    second = await sender.start("om_message", label="video")
    await sender.stop("om_message", first, label="card")
    await sender.stop("om_message", second, label="video")

    assert first == "reaction_1"
    assert second == "reaction_2"
    assert [req.paths["reaction_id"] for req in client.message_reaction.delete_requests] == [
        "reaction_1",
        "reaction_2",
    ]
