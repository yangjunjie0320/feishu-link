"""Read public counts only from the requested YouTube watch-page payload."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

_COMMENTS_PANELS = {"comment-item-section", "engagement-panel-comments-section"}
_NUMBER = re.compile(
    r"(?<![\w.\-])\d+(?:[,.\u00a0\u202f ]\d+)*(?:\s*(?:[KMB](?![A-Za-z])|[万亿]))?", re.I,
)
_SCALES = {"k": 1000, "m": 1_000_000, "b": 1_000_000_000, "万": 10_000, "亿": 100_000_000}


def public_count(value: Any) -> int | None:
    """Accept one public count, including localized grouping and compact labels."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if not isinstance(value, str):
        return None
    if re.search(r"[-+]\s*\d", value):
        return None
    matches = list(_NUMBER.finditer(value.strip()))
    if len(matches) != 1:
        return None
    raw = re.sub(r"[\s\u00a0\u202f]", "", matches[0].group())
    scale = _SCALES.get(raw[-1:].lower())
    if scale is not None:
        raw = raw[:-1]
        # A decimal comma is used by some language variants for compact counts.
        if "," in raw and "." not in raw and len(raw.rsplit(",", 1)[-1]) != 3:
            raw = raw.replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif re.fullmatch(r"\d{1,3}(?:[,.]\d{3})+", raw):
        raw = raw.replace(",", "").replace(".", "")
    elif not raw.isdecimal():
        return None
    try:
        count = Decimal(raw) * (scale or 1)
        return int(count) if count >= 0 and count == count.to_integral_value() else None
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _at(value: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _items(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    simple = _at(value, "simpleText")
    if isinstance(simple, str):
        return simple
    return "".join(
        item["text"] for item in _items(_at(value, "runs"))
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    )


def _first_count(*values: Any) -> int | None:
    for value in values:
        count = public_count(_text(value) if isinstance(value, dict) else value)
        if count is not None:
            return count
    return None


def _like_count(button: Any) -> int | None:
    model = _at(button, "segmentedLikeDislikeButtonViewModel", "likeButtonViewModel")
    if isinstance(model, dict):
        # Current web clients wrap both likeButtonViewModel and toggleButtonViewModel twice.
        for _ in range(6):
            child = next((model[key] for key in ("likeButtonViewModel", "toggleButtonViewModel")
                          if isinstance(model.get(key), dict)), None)
            if child is None:
                break
            model = child
        default = _at(model, "defaultButtonViewModel", "buttonViewModel")
        return _first_count(_at(default, "accessibilityText"), _at(default, "title"))
    legacy = _at(
        button, "segmentedLikeDislikeButtonRenderer", "likeButton", "toggleButtonRenderer",
    )
    if not isinstance(legacy, dict):
        legacy = _at(button, "toggleButtonRenderer")
        if _at(legacy, "defaultIcon", "iconType") != "LIKE":
            return None
    return _first_count(
        _at(legacy, "defaultText", "accessibility", "accessibilityData", "label"),
        _at(legacy, "accessibility", "label"),
        _at(legacy, "accessibilityData", "accessibilityData", "label"),
        _at(legacy, "defaultText"),
    )


def youtube_page_metrics(payload: Any, video_id: str) -> dict[str, int]:
    """A next response needs its own target identity; another payload cannot vouch for it."""
    if _at(payload, "currentVideoEndpoint", "watchEndpoint", "videoId") != video_id:
        return {}
    results: dict[str, int] = {}
    contents = _items(_at(
        payload, "contents", "twoColumnWatchNextResults", "results", "results", "contents",
    ))
    for item in contents:
        primary = _at(item, "videoPrimaryInfoRenderer")
        if isinstance(primary, dict):
            view = _at(primary, "viewCount", "videoViewCountRenderer")
            if not _at(view, "isLive"):
                count = _first_count(_at(view, "originalViewCount"), _at(view, "viewCount"))
                if count is not None:
                    results.setdefault("view_count", count)
            for button in _items(_at(primary, "videoActions", "menuRenderer", "topLevelButtons")):
                count = _like_count(button)
                if count is not None:
                    results.setdefault("like_count", count)
        for child in _items(_at(item, "itemSectionRenderer", "contents")):
            count = _first_count(_at(child, "commentsEntryPointHeaderRenderer", "commentCount"))
            if count is not None:
                results.setdefault("comment_count", count)
    for item in _items(_at(payload, "engagementPanels")):
        panel = _at(item, "engagementPanelSectionListRenderer")
        panel_id = _at(panel, "panelIdentifier")
        if not isinstance(panel_id, str) or panel_id not in _COMMENTS_PANELS:
            continue
        count = _first_count(_at(
            panel, "header", "engagementPanelTitleHeaderRenderer", "contextualInfo",
        ))
        if count is not None:
            results.setdefault("comment_count", count)
    return results
