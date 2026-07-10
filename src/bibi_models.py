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
class SubtitleSegment:
    index: int
    start_time: float
    end_time: float
    text: str
    speaker_id: int | None = None


@dataclass(frozen=True)
class SubtitleFetchResult:
    subtitles: tuple[SubtitleSegment, ...]
    status: str
    source: str
    reason: str = ""

    @property
    def available(self) -> bool:
        return bool(self.subtitles)


@dataclass(frozen=True)
class SummaryResult:
    content: str
    model: str
    usage: Usage
    from_cache: bool
    video_url: str
    content_id: str = ""
    subtitles: tuple[SubtitleSegment, ...] = ()

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
            subtitles=subtitle_segments_from_web_response(data),
        )


def subtitle_segments_from_web_response(data: dict[str, Any]) -> tuple[SubtitleSegment, ...]:
    """Parse the first populated subtitles array from known BibiGPT containers."""
    for candidate in _response_containers(data):
        raw_subtitles = (
            candidate.get("subtitlesArray")
            or candidate.get("subtitles_array")
            or candidate.get("subtitles")
        )
        if not isinstance(raw_subtitles, list):
            continue

        subtitles = _parse_subtitle_array(raw_subtitles)
        if subtitles:
            return subtitles
    return ()


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


def _parse_subtitle_array(raw_subtitles: list[Any]) -> tuple[SubtitleSegment, ...]:
    subtitles: list[SubtitleSegment] = []
    used_indices: set[int] = set()
    for position, raw in enumerate(raw_subtitles):
        if not isinstance(raw, dict):
            continue
        text = raw.get("text")
        if not isinstance(text, str) or not text.strip():
            continue

        index = _as_int(raw.get("index"), position)
        while index in used_indices:
            index += 1
        used_indices.add(index)

        start_time = _as_float(raw.get("startTime", raw.get("start")), 0.0)
        end_time = _as_float(raw.get("end", raw.get("endTime")), start_time)
        subtitles.append(
            SubtitleSegment(
                index=index,
                start_time=start_time,
                end_time=end_time,
                text=text.strip(),
                speaker_id=_as_optional_int(raw.get("speaker_id", raw.get("speakerId"))),
            )
        )
    return tuple(subtitles)


def _as_int(value: Any, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback


def _as_optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _as_float(value: Any, fallback: float) -> float:
    if isinstance(value, bool):
        return fallback
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return result if math.isfinite(result) else fallback
