from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import lark_oapi as lark

from src import log
from src.config import Settings
from src.listener import LarkEventListener
from src.pipeline import Pipeline


async def _run(settings: Settings) -> None:
    logger = logging.getLogger(__name__)

    if not settings.app_id or not settings.app_secret:
        raise RuntimeError("app_id and app_secret must be configured")

    lark_client = (
        lark.Client.builder()
        .app_id(settings.app_id)
        .app_secret(settings.app_secret)
        .build()
    )

    listener = LarkEventListener(settings)
    pipeline = Pipeline(settings, lark_client)

    logger.info("feishu-link started (mode=%s)", settings.mode.value)
    async for event in listener.listen():
        await pipeline.handle(event)


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
