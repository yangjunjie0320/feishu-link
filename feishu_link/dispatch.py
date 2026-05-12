from __future__ import annotations

import httpx

from .config import Settings
from .parsers.base import LinkMetadata, Parser, ParserError
from .parsers.fallback import FallbackParser
from .parsers.og_meta import OGMetaParser
from .parsers.youtube import YouTubeParser, is_youtube_url


class Dispatcher:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._youtube = YouTubeParser(client, api_key=settings.youtube_api_key)
        self._og = OGMetaParser(client)
        self._fallback = FallbackParser(client)

    async def parse(self, url: str) -> LinkMetadata:
        parser: Parser
        parser = self._youtube if is_youtube_url(url) else self._og

        try:
            return await parser.parse(url)
        except ParserError:
            return await self._fallback.parse(url)
