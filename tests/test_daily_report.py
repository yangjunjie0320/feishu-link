import json
from datetime import date
from unittest.mock import AsyncMock, MagicMock

from src.archive_store import ArchivedRow
from src.card import card_message_wire_size
from src.config import Settings
from src.daily_report import (
    DailyReporter,
    build_report_card,
    build_report_markdown,
    seconds_until,
)


def _row(title: str, platform: str, duration: str = "", recorded: str = "") -> ArchivedRow:
    return ArchivedRow(
        title=title,
        url=f"https://example.com/{title}",
        platform=platform,
        channel="",
        duration=duration,
        sender="张三",
        chat="链接群",
        recorded_at=recorded or "2026-07-21 10:00",
    )


def test_seconds_until_is_within_next_24h() -> None:
    for hhmm in ("00:00", "12:30", "23:59"):
        seconds = seconds_until(hhmm, "Asia/Shanghai")
        assert 0 < seconds <= 24 * 3600


def test_build_report_markdown_groups_by_platform() -> None:
    rows = [
        _row("a", "youtube", duration="3:05"),
        _row("b", "bilibili"),
        _row("c", "youtube"),
    ]

    md = build_report_markdown(date(2026, 7, 21), rows)

    assert md.startswith("共 3 条 · youtube 2 · bilibili 1")
    assert md.index("- youtube（2）") < md.index("- bilibili（1）")
    assert "  - [a](https://example.com/a) (3:05)" in md
    assert "  - [b](https://example.com/b)\n" in md


def test_build_report_markdown_truncates_per_platform() -> None:
    rows = [_row(f"v{i}", "youtube") for i in range(10)]

    md = build_report_markdown(date(2026, 7, 21), rows, max_per_platform=3)

    assert "  - [v2]" in md
    assert "  - [v3]" not in md
    assert "另有 7 条见表格" in md


def test_build_report_card_stays_under_wire_limit() -> None:
    rows = [_row("标题很长" * 30 + str(i), "youtube") for i in range(400)]

    card_json = build_report_card(date(2026, 7, 21), rows)

    assert card_message_wire_size(card_json) <= 24 * 1024
    assert "另有" in json.dumps(json.loads(card_json), ensure_ascii=False)


async def test_send_report_skips_empty_day() -> None:
    archive = MagicMock()
    archive.fetch_day = AsyncMock(return_value=[])
    sender = MagicMock()
    sender.send_to_chat = AsyncMock()
    reporter = DailyReporter(Settings(archive_chat_id="oc_a"), archive, sender)

    assert await reporter.send_report(date(2026, 7, 21)) is False
    sender.send_to_chat.assert_not_awaited()


async def test_send_report_falls_back_to_archive_chat() -> None:
    archive = MagicMock()
    archive.fetch_day = AsyncMock(return_value=[_row("a", "youtube")])
    sender = MagicMock()
    sender.send_to_chat = AsyncMock(return_value=True)
    reporter = DailyReporter(Settings(archive_chat_id="oc_a"), archive, sender)

    assert await reporter.send_report(date(2026, 7, 21)) is True
    assert sender.send_to_chat.await_args.args[1] == "oc_a"


async def test_send_report_fans_out_to_every_chat() -> None:
    archive = MagicMock()
    archive.fetch_day = AsyncMock(return_value=[_row("a", "youtube")])
    sender = MagicMock()
    # First chat fails after retries, second succeeds: report still counts as sent.
    sender.send_to_chat = AsyncMock(side_effect=[False, True])
    settings = Settings(report_chat_id="oc_r", report_chat_ids=["oc_x"])
    reporter = DailyReporter(settings, archive, sender)

    assert await reporter.send_report(date(2026, 7, 21)) is True
    targets = [call.args[1] for call in sender.send_to_chat.await_args_list]
    assert targets == ["oc_r", "oc_x"]
    cards = {call.args[0] for call in sender.send_to_chat.await_args_list}
    assert len(cards) == 1


async def test_send_report_without_target_chat_skips() -> None:
    archive = MagicMock()
    archive.fetch_day = AsyncMock()
    sender = MagicMock()
    reporter = DailyReporter(Settings(), archive, sender)

    assert await reporter.send_report(date(2026, 7, 21)) is False
    archive.fetch_day.assert_not_awaited()
