from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta

from dateutil import tz

from .archive_store import ArchivedRow, BitableArchive
from .card import build_markdown_card, card_message_wire_size
from .config import Settings
from .sender import CardSender

logger = logging.getLogger(__name__)

# Target well under Feishu's 30 KiB hard limit for one card message.
_MAX_WIRE_BYTES = 24 * 1024
_TRUNCATION_STEPS = (0, 30, 20, 10, 5, 3, 1)  # 0 = unlimited


def seconds_until(hhmm: str, tz_name: str) -> float:
    """Seconds from now to the next local occurrence of HH:MM in tz_name."""
    zone = tz.gettz(tz_name)
    if zone is None:
        raise ValueError(f"unknown timezone: {tz_name}")
    hour, minute = (int(part) for part in hhmm.split(":"))
    now = datetime.now(tz=zone)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def build_report_markdown(day: date, rows: list[ArchivedRow], max_per_platform: int = 0) -> str:
    """Platforms as top-level bullets with entries indented beneath: this is
    the section protocol build_markdown_card(sectioned=True) splits into one
    collapsible panel per platform."""
    grouped: dict[str, list[ArchivedRow]] = defaultdict(list)
    for row in rows:
        grouped[row.platform or "other"].append(row)
    platforms = sorted(grouped, key=lambda p: (-len(grouped[p]), p))

    distribution = " · ".join(f"{p} {len(grouped[p])}" for p in platforms)
    lines = [f"共 {len(rows)} 条 · {distribution}", ""]
    for platform in platforms:
        entries = grouped[platform]
        shown = entries if max_per_platform <= 0 else entries[:max_per_platform]
        lines.append(f"- {platform}（{len(entries)}）")
        for row in shown:
            suffix = f" ({row.duration})" if row.duration else ""
            title = row.title or row.url
            lines.append(f"  - [{title}]({row.url}){suffix}")
        hidden = len(entries) - len(shown)
        if hidden > 0:
            lines.append(f"  - （另有 {hidden} 条见表格）")
    return "\n".join(lines).rstrip() + "\n"


def build_report_card(day: date, rows: list[ArchivedRow]) -> str:
    for cap in _TRUNCATION_STEPS:
        markdown = build_report_markdown(day, rows, max_per_platform=cap)
        card_json = build_markdown_card(
            f"链接日报 {day.isoformat()}",
            markdown,
            collapsed=True,
            sectioned=True,
        )
        if card_message_wire_size(card_json) <= _MAX_WIRE_BYTES:
            return card_json
    return card_json


class DailyReporter:
    def __init__(self, settings: Settings, archive: BitableArchive, sender: CardSender) -> None:
        self._settings = settings
        self._archive = archive
        self._sender = sender

    async def run(self) -> None:
        logger.info(
            "daily report scheduler started: time=%s tz=%s",
            self._settings.report_time,
            self._settings.report_timezone,
        )
        while True:
            await asyncio.sleep(
                seconds_until(self._settings.report_time, self._settings.report_timezone)
            )
            try:
                await self.send_report()
            except Exception as e:
                logger.error("daily report failed: %s", e)
            # Skip past the trigger minute so one firing never double-sends.
            await asyncio.sleep(60)

    async def send_report(self, day: date | None = None) -> bool:
        if day is None:
            zone = tz.gettz(self._settings.report_timezone)
            day = datetime.now(tz=zone).date()
        chat_id = self._settings.effective_report_chat_id()
        if not chat_id:
            logger.warning("daily report skipped: no report_chat_id or archive_chat_id")
            return False

        rows = await self._archive.fetch_day(day)
        if not rows:
            logger.info("daily report skipped: no archived links for %s", day.isoformat())
            return False

        card_json = build_report_card(day, rows)
        sent = await self._sender.send_to_chat(card_json, chat_id)
        if sent:
            logger.info(
                "daily report sent: day=%s entries=%d chat_id=%s",
                day.isoformat(),
                len(rows),
                chat_id,
            )
        return sent
