import json
from types import SimpleNamespace

from src.listener import _coerce_action_value, _parse_card_action_event, _parse_event


def _message_data(**msg_overrides) -> SimpleNamespace:
    msg = SimpleNamespace(
        message_id="om_msg",
        chat_id="oc_chat",
        chat_type="group",
        message_type="text",
        content='{"text": "hello"}',
        mentions=None,
        root_id="",
        parent_id="",
    )
    for key, value in msg_overrides.items():
        setattr(msg, key, value)
    return SimpleNamespace(
        event=SimpleNamespace(
            message=msg,
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_sender")),
        )
    )


def test_parse_event_captures_reply_context() -> None:
    event = _parse_event(_message_data(root_id="om_root", parent_id="om_parent"))

    assert event is not None
    assert event.root_id == "om_root"
    assert event.parent_id == "om_parent"


def test_parse_event_defaults_reply_context_to_empty() -> None:
    event = _parse_event(_message_data(root_id=None, parent_id=None))

    assert event is not None
    assert event.root_id == ""
    assert event.parent_id == ""


def test_parse_card_action_event_from_dict_value() -> None:
    data = SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(value={"action": "download_video", "url": "https://b23.tv/abc"}),
            context=SimpleNamespace(open_message_id="om_card", open_chat_id="oc_chat"),
            operator=SimpleNamespace(open_id="ou_user"),
        )
    )

    event = _parse_card_action_event(data)

    assert event is not None
    assert event.action == "download_video"
    assert event.source_url == "https://b23.tv/abc"
    assert event.message_id == "om_card"
    assert event.chat_id == "oc_chat"
    assert event.operator_open_id == "ou_user"


def test_parse_card_action_event_from_json_value() -> None:
    data = SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(
                value=json.dumps(
                    {
                        "action": "download_video",
                        "source_url": "https://youtu.be/abc",
                    }
                )
            ),
            context=SimpleNamespace(open_message_id="om_card", open_chat_id="oc_chat"),
            operator=SimpleNamespace(open_id="ou_user"),
        )
    )

    event = _parse_card_action_event(data)

    assert event is not None
    assert event.source_url == "https://youtu.be/abc"


def test_parse_card_summary_action_event() -> None:
    data = SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(
                value={"action": "summarize_video", "url": "https://b23.tv/abc"}
            ),
            context=SimpleNamespace(open_message_id="om_card", open_chat_id="oc_chat"),
            operator=SimpleNamespace(open_id="ou_user"),
        )
    )

    event = _parse_card_action_event(data)

    assert event is not None
    assert event.action == "summarize_video"
    assert event.source_url == "https://b23.tv/abc"


def test_parse_card_comment_analysis_action_event() -> None:
    data = SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(
                value={"action": "analyze_comments", "url": "https://b23.tv/abc"}
            ),
            context=SimpleNamespace(open_message_id="om_card", open_chat_id="oc_chat"),
            operator=SimpleNamespace(open_id="ou_user"),
        )
    )

    event = _parse_card_action_event(data)

    assert event is not None
    assert event.action == "analyze_comments"
    assert event.source_url == "https://b23.tv/abc"


def test_parse_card_action_event_rejects_missing_url() -> None:
    data = SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(value={"action": "download_video"}),
            context=SimpleNamespace(open_message_id="om_card", open_chat_id="oc_chat"),
            operator=SimpleNamespace(open_id="ou_user"),
        )
    )

    assert _parse_card_action_event(data) is None


def test_coerce_action_value_ignores_invalid_json() -> None:
    assert _coerce_action_value("not json") == {}
