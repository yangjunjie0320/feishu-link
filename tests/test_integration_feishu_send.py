from __future__ import annotations

import os
from pathlib import Path

import lark_oapi as lark
import pytest

from feishu_link.card import build_card
from feishu_link.config import Mode, Settings
from feishu_link.parsers.base import LinkMetadata
from feishu_link.sender import CardSender
from feishu_link.time_utils import format_beijing, now_utc


def _integration_enabled() -> bool:
    return os.getenv("FEISHU_LINK_INTEGRATION_SEND") == "1"


@pytest.mark.integration
@pytest.mark.skipif(
    not _integration_enabled(),
    reason="set FEISHU_LINK_INTEGRATION_SEND=1 to send a real Feishu card",
)
async def test_send_real_card_to_feishu_archive() -> None:
    config_path = Path(os.getenv("FEISHU_LINK_CONFIG_PATH", "config.yaml"))
    if not config_path.exists():
        pytest.fail(f"config file not found: {config_path}")

    settings = Settings.from_yaml(config_path)
    if not settings.app_id or not settings.app_secret:
        pytest.fail("app_id and app_secret must be configured")
    message_id = os.getenv("FEISHU_LINK_INTEGRATION_MESSAGE_ID", "")
    if settings.mode == Mode.A and not message_id:
        pytest.fail("FEISHU_LINK_INTEGRATION_MESSAGE_ID must be set for mode A")
    if settings.mode == Mode.B and not settings.archive_chat_id:
        pytest.fail("archive_chat_id must be configured for mode B")

    client = (
        lark.Client.builder()
        .app_id(settings.app_id)
        .app_secret(settings.app_secret)
        .build()
    )
    sender = CardSender(settings, client)

    sent_at = format_beijing(now_utc())
    meta = LinkMetadata(
        source_url="https://example.com/",
        title=f"feishu-link integration test {sent_at}",
        site_name="Integration",
        channel="pytest",
    )

    if settings.mode == Mode.A:
        await sender._reply(build_card(meta), message_id)
    else:
        await sender._send_to_archive(build_card(meta))
