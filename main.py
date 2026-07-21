from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import lark_oapi as lark

from src import log
from src.archive_store import BitableArchive, ChatDirectory
from src.config import Settings
from src.daily_report import DailyReporter
from src.listener import LarkEventListener, ListenerEvent
from src.pipeline import Pipeline
from src.sender import CardSender

logger = logging.getLogger(__name__)


async def _handle_safely(pipeline: Pipeline, event: ListenerEvent) -> None:
    """Handle one event, logging any error so a fire-and-forget task never
    silently swallows it and one failure cannot crash the event loop."""
    try:
        await pipeline.handle(event)
    except Exception:
        logger.exception("unhandled error handling event")


async def _run(settings: Settings) -> None:
    lark_client = _build_lark_client(settings)

    listener = LarkEventListener(settings)

    tasks: set[asyncio.Task[None]] = set()

    archive = None
    if settings.bitable_enabled:
        archive = BitableArchive(settings, lark_client, ChatDirectory(lark_client))
        consumer = asyncio.create_task(archive.run())
        tasks.add(consumer)
        consumer.add_done_callback(tasks.discard)

    pipeline = Pipeline(settings, lark_client, archive=archive)

    if settings.report_enabled:
        if archive is None:
            raise RuntimeError("report_enabled requires bitable_enabled")
        reporter = DailyReporter(settings, archive, CardSender(settings, lark_client))
        report_task = asyncio.create_task(reporter.run())
        tasks.add(report_task)
        report_task.add_done_callback(tasks.discard)

    logger.info("feishu-link started (mode=%s)", settings.mode.value)
    # Each event runs as its own task so a slow handler (comment analysis,
    # summary, download) never head-of-line blocks other messages.
    async for event in listener.listen():
        task = asyncio.create_task(_handle_safely(pipeline, event))
        tasks.add(task)
        task.add_done_callback(tasks.discard)


def _build_lark_client(settings: Settings) -> lark.Client:
    if not settings.app_id or not settings.app_secret:
        raise RuntimeError("app_id and app_secret must be configured")
    return lark.Client.builder().app_id(settings.app_id).app_secret(settings.app_secret).build()


async def _init_bitable(settings: Settings) -> None:
    client = _build_lark_client(settings)
    archive = BitableArchive(settings, client, ChatDirectory(client))
    app_token, table_id = await archive.bootstrap()
    logger.info("bitable created: app_token=%s table_id=%s", app_token, table_id)
    logger.info("table url: https://feishu.cn/base/%s", app_token)
    logger.info("fill bitable_app_token/bitable_table_id into config.yaml to enable archiving")


async def _send_report(settings: Settings, day_arg: str | None) -> bool:
    from datetime import date

    client = _build_lark_client(settings)
    archive = BitableArchive(settings, client, ChatDirectory(client))
    reporter = DailyReporter(settings, archive, CardSender(settings, client))
    day = date.fromisoformat(day_arg) if day_arg else None
    return await reporter.send_report(day)


def main() -> None:
    parser = argparse.ArgumentParser(description="Feishu link secretary")
    parser.add_argument("--config", metavar="PATH", help="YAML config file path")
    parser.add_argument(
        "--browser-login",
        metavar="PLATFORM",
        help="Open a headed browser to log in once and seed a cookie-refresh profile",
    )
    parser.add_argument(
        "--init-bitable",
        action="store_true",
        help="One-time: create the archive bitable base/table and print its tokens",
    )
    parser.add_argument(
        "--send-report",
        action="store_true",
        help="Send the daily report once and exit (for verification)",
    )
    parser.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help="Report day for --send-report (default: today in report_timezone)",
    )
    args = parser.parse_args()

    settings = Settings.from_yaml(args.config) if args.config else Settings()

    log.setup(level=settings.log_level, log_dir=settings.log_dir)

    if args.browser_login:
        from src.cookie_refresh import browser_login

        ok = asyncio.run(browser_login(args.browser_login, settings))
        sys.exit(0 if ok else 1)

    if args.init_bitable:
        asyncio.run(_init_bitable(settings))
        sys.exit(0)

    if args.send_report:
        ok = asyncio.run(_send_report(settings, args.date))
        sys.exit(0 if ok else 1)

    try:
        asyncio.run(_run(settings))
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logging.getLogger(__name__).critical("fatal: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
