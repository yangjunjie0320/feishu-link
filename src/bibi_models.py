from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class ChapterSummarySection:
    index: int
    start_time: float
    end_time: float
    title: str
    summary: str


@dataclass(frozen=True)
class ChapterSummaryFetchResult:
    introduction: str
    sections: tuple[ChapterSummarySection, ...]
    status: str
    source: str
    reason: str = ""

    @property
    def available(self) -> bool:
        return bool(self.sections)


@dataclass(frozen=True)
class SummaryResult:
    content: str
    model: str
    usage: Usage
    from_cache: bool
    video_url: str
    content_id: str = ""

    @classmethod
    def from_web_response(cls, data: dict[str, Any], video_url: str) -> SummaryResult:
        content = data.get("summary") or data.get("content") or ""
        detail = data.get("detail", {})
        if not content and isinstance(detail, dict):
            content = detail.get("summary", "")
        if not content:
            raise ValueError("BibiGPT web response contains empty summary")

        return cls(
            content=content,
            model=data.get("model", "bibigpt-web"),
            usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
            from_cache=bool(data.get("fromCache") or data.get("from_cache") or data.get("cached")),
            video_url=video_url,
            content_id=_extract_content_id(data),
        )


def parse_chapter_summary_response(
    data: dict[str, Any],
) -> tuple[str, tuple[ChapterSummarySection, ...]]:
    """Parse only the top-level BibiGPT timeline chapter-summary fields."""
    raw_introduction = data.get("chapterSummary")
    introduction = raw_introduction.strip() if isinstance(raw_introduction, str) else ""
    raw_chapters = data.get("chapters")
    if not isinstance(raw_chapters, list):
        return introduction, ()

    sections: list[ChapterSummarySection] = []
    for position, raw in enumerate(raw_chapters):
        if not isinstance(raw, dict):
            continue
        start_time = _as_finite_float(raw.get("start"))
        end_time = _as_finite_float(raw.get("end"))
        title = raw.get("title")
        summary = raw.get("summary")
        if (
            start_time is None
            or end_time is None
            or start_time < 0
            or end_time < start_time
            or not isinstance(title, str)
            or not title.strip()
            or not isinstance(summary, str)
            or not summary.strip()
        ):
            continue
        sections.append(
            ChapterSummarySection(
                index=position,
                start_time=start_time,
                end_time=end_time,
                title=title.strip(),
                summary=summary.strip(),
            )
        )
    return introduction, tuple(sections)


def _extract_content_id(data: dict[str, Any]) -> str:
    containers = _response_containers(data)
    for keys in (("contentId", "content_id"), ("dbId",)):
        for candidate in containers:
            value = next((candidate.get(key) for key in keys if candidate.get(key)), None)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, int) and not isinstance(value, bool):
                return str(value)
    return ""


def _response_containers(data: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    containers: list[dict[str, Any]] = []
    pending = [data]
    seen: set[int] = set()
    while pending:
        candidate = pending.pop(0)
        identity = id(candidate)
        if identity in seen:
            continue
        seen.add(identity)
        containers.append(candidate)
        for key in ("detail", "videoDetail"):
            nested = candidate.get(key)
            if isinstance(nested, dict):
                pending.append(nested)
    return tuple(containers)


def _as_finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None
