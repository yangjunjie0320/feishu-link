"""Preserve the total duration already present in Instagram's native DASH data."""

from __future__ import annotations

import logging
import math
import re
from typing import TYPE_CHECKING, Any
from xml.etree import ElementTree

if TYPE_CHECKING:
    from yt_dlp import YoutubeDL

logger = logging.getLogger(__name__)

_MPD_NAMESPACE = "urn:mpeg:dash:schema:mpd:2011"
_DURATION = re.compile(
    r"P(?:(\d+)D)?"
    r"(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?"
)


def instagram_mpd_duration(manifest: object) -> float | None:
    """Read a finite, positive total from a static MPD; never estimate fragments."""
    if not isinstance(manifest, str) or len(manifest) > 1_000_000:
        return None
    upper = manifest.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        return None
    try:
        root = ElementTree.fromstring(manifest)
    except (ElementTree.ParseError, ValueError, LookupError):
        return None
    if root.tag not in {"MPD", f"{{{_MPD_NAMESPACE}}}MPD"}:
        return None
    if root.get("type", "static") != "static":
        return None
    value = root.get("mediaPresentationDuration", "")
    match = _DURATION.fullmatch(value)
    if match is None or not any(match.groups()):
        return None
    if "T" in value and not any(match.groups()[1:]):
        return None
    try:
        days, hours, minutes, seconds = (float(value or 0) for value in match.groups())
        duration = days * 86400 + hours * 3600 + minutes * 60 + seconds
    except (ValueError, OverflowError):
        return None
    return duration if math.isfinite(duration) and duration > 0 else None


def instagram_duration_seconds(value: object) -> int | None:
    """Round up fractional seconds so a video above the limit remains above it."""
    if value is None or isinstance(value, bool):
        return None
    try:
        duration = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return math.ceil(duration) if math.isfinite(duration) and duration > 0 else None


def install_instagram_duration_fallback(ydl: YoutubeDL) -> None:
    """Wrap this worker's extractor instance; other instances and platforms are untouched."""
    extractor = ydl.get_info_extractor("Instagram")
    extract_media = extractor._extract_product_media

    def extract_media_with_duration(product_media: dict[str, Any]) -> dict[str, Any]:
        info = extract_media(product_media)
        if info.get("duration") is None:
            duration = instagram_mpd_duration(product_media.get("video_dash_manifest"))
            if duration is not None:
                info["duration"] = duration
                logger.info(
                    "instagram duration recovered: id=%s source=static_mpd seconds=%s",
                    info.get("id"), duration,
                )
        return info

    extractor._extract_product_media = extract_media_with_duration
