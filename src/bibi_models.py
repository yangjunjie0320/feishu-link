from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Usage:
        return cls(
            prompt_tokens=data.get("prompt_tokens", 0),
            completion_tokens=data.get("completion_tokens", 0),
            total_tokens=data.get("total_tokens", 0),
        )


@dataclass(frozen=True)
class SummaryResult:
    content: str
    model: str
    usage: Usage
    from_cache: bool
    video_url: str

    @classmethod
    def from_response(cls, data: dict[str, Any], video_url: str) -> SummaryResult:
        choices = data.get("choices", [])
        if not choices:
            raise ValueError("API response contains no choices")

        content = choices[0].get("message", {}).get("content", "")
        if not content:
            raise ValueError("API response contains empty content")

        return cls(
            content=content,
            model=data.get("model", "unknown"),
            usage=Usage.from_dict(data.get("usage", {})),
            from_cache=data.get("from_cache", False),
            video_url=video_url,
        )

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
        )
