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
    if not settings.app_id or not settings.app_secret:
        raise RuntimeError("app_id and app_secret must be configured")

    lark_client = (
        lark.Client.builder().app_id(settings.app_id).app_secret(settings.app_secret).build()
    )

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


def main() -> None:
    parser = argparse.ArgumentParser(description="Feishu link secretary")
    parser.add_argument("--config", metavar="PATH", help="YAML config file path")
    parser.add_argument(
        "--browser-login",
        metavar="PLATFORM",
        help="Open a headed browser to log in once and seed a cookie-refresh profile",
    )
    args = parser.parse_args()

    settings = Settings.from_yaml(args.config) if args.config else Settings()

    log.setup(level=settings.log_level, log_dir=settings.log_dir)

    if args.browser_login:
        from src.cookie_refresh import browser_login

        ok = asyncio.run(browser_login(args.browser_login, settings))
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
