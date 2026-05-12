from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from ..time_utils import now_utc


class ParserError(Exception):
    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"parse failed: url={url} reason={reason}")
        self.url = url
        self.reason = reason


@dataclass
class LinkMetadata:
    source_url: str
    title: str = ""
    description: str = ""
    cover_url: str = ""
    site_name: str = ""
    duration_seconds: int | None = None
    channel: str | None = None
    fetched_at_utc: datetime = field(default_factory=now_utc)


@runtime_checkable
class Parser(Protocol):
    async def parse(self, url: str) -> LinkMetadata: ...
