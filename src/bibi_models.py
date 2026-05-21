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
