import json
from types import SimpleNamespace

from src.listener import _coerce_action_value, _parse_card_action_event


def test_parse_card_action_event_from_dict_value() -> None:
    data = SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(
                value={"action": "download_video", "url": "https://b23.tv/abc"}
            ),
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
                value=json.dumps({
                    "action": "download_video",
                    "source_url": "https://youtu.be/abc",
                })
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
