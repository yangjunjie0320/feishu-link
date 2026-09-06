from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from ..time_utils import now_utc


class ParserError(Exception):
    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"parse failed: url={url} reason={reason}")
        self.url = url
        self.reason = reason


class MediaType(StrEnum):
    ARTICLE = "article"
    VIDEO = "video"
    UNKNOWN = "unknown"


class CardStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"


@dataclass
class DownloadCandidate:
    url: str
    format_id: str = ""
    ext: str = ""
    filesize: int | None = None


@dataclass
class LinkMetadata:
    source_url: str
    title: str = ""
    translated_title: str = ""
    description: str = ""
    translated_description: str = ""
    cover_url: str = ""
    site_name: str = ""
    platform: str = "web"
    canonical_url: str = ""
    media_type: MediaType = MediaType.ARTICLE
    duration_seconds: int | None = None
    channel: str | None = None
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    repost_count: int | None = None
    download_candidates: list[DownloadCandidate] = field(default_factory=list)
    requires_auth: bool = False
    parse_warnings: list[str] = field(default_factory=list)
    fetched_at_utc: datetime = field(default_factory=now_utc)
    cover_candidates: list[str] = field(default_factory=list)
    # False means a source confirmed a text-only post; None means unknown.
    has_visual: bool | None = None
    content_verified: bool = False


@dataclass
class CardParseResult:
    metadata: LinkMetadata
    status: CardStatus = CardStatus.UNAVAILABLE
    reason: str = ""
    sources: list[str] = field(default_factory=list)
    has_content: bool = False


@runtime_checkable
class Parser(Protocol):
    async def parse(self, url: str) -> LinkMetadata: ...
