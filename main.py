from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys

from feishu_link import log
from feishu_link.config import Settings
from feishu_link.listener import LarkEventListener
from feishu_link.pipeline import Pipeline


def _get_self_open_id(lark_cli_bin: str) -> str:
    try:
        result = subprocess.run(
            [lark_cli_bin, "auth", "status", "--format", "json"],
            capture_output=True,
            text=True,
            check=True,
        )
        data: dict = json.loads(result.stdout)
        open_id: str = data.get("open_id") or data.get("openId") or ""
        if not open_id:
            # Try nested structure
            users = data.get("users", [])
            if users:
                open_id = users[0].get("open_id", "")
        return open_id
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError) as e:
        raise RuntimeError(f"failed to get self open_id from lark-cli: {e}") from e


async def _run(settings: Settings) -> None:
    logger = logging.getLogger(__name__)

    if not settings.self_open_id:
        logger.info("querying self open_id from lark-cli auth status")
        settings.self_open_id = _get_self_open_id(settings.lark_cli_bin)
        if not settings.self_open_id:
            raise RuntimeError(
                "could not determine self open_id. "
                "Ensure lark-cli is authenticated (lark-cli auth login --recommend)"
            )
        logger.info("self open_id resolved: %s", settings.self_open_id)

    listener = LarkEventListener(settings)
    pipeline = Pipeline(settings)

    logger.info("feishu-link started (mode=%s)", settings.mode.value)
    async for event in listener.listen():
        await pipeline.handle(event)


def main() -> None:
    parser = argparse.ArgumentParser(description="Feishu link secretary")
    parser.add_argument("--config", metavar="PATH", help="YAML config file path")
    args = parser.parse_args()

    settings = Settings.from_yaml(args.config) if args.config else Settings()

    log.setup(level=settings.log_level, log_dir=settings.log_dir)

    try:
        asyncio.run(_run(settings))
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logging.getLogger(__name__).critical("fatal: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
