from __future__ import annotations

import math

import pytest
from yt_dlp import YoutubeDL

from src.config import Settings
from src.media_downloader import explain_video_skip
from src.parsers.instagram_duration import (
    instagram_duration_seconds,
    instagram_mpd_duration,
    install_instagram_duration_fallback,
)
from src.parsers.ytdlp import _metadata_from_info

_URL = "https://www.instagram.com/reel/Dbuti2PMeW6/"


def _manifest(duration: str = "PT13.350022S", kind: str = "static") -> str:
    return (
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" '
        f'type="{kind}" mediaPresentationDuration="{duration}">'
        '<Period><AdaptationSet mimeType="video/mp4"><Representation id="1" '
        'bandwidth="123456" codecs="avc1.4d401e">'
        '<BaseURL>https://cdn.example/video.mp4</BaseURL>'
        '</Representation></AdaptationSet></Period></MPD>'
    )


def _product(**fields: object) -> dict:
    return {"pk": "3899934464823415648", "video_dash_manifest": _manifest(), **fields}


def test_real_instagram_extractor_preserves_previously_discarded_mpd_duration() -> None:
    with YoutubeDL({"quiet": True}) as ydl:
        extractor = ydl.get_info_extractor("Instagram")
        before = extractor._extract_product_media(_product())
        assert before["duration"] is None
        assert before["formats"]

        install_instagram_duration_fallback(ydl)
        after = extractor._extract_product_media(_product())

    assert after["duration"] == pytest.approx(13.350022)
    assert after["formats"] == before["formats"]
    assert _metadata_from_info(_URL, "instagram", after).duration_seconds == 14


def test_existing_native_duration_takes_precedence_and_other_instances_are_unchanged() -> None:
    with YoutubeDL({"quiet": True}) as patched, YoutubeDL({"quiet": True}) as untouched:
        install_instagram_duration_fallback(patched)
        extractor = patched.get_info_extractor("Instagram")
        native = extractor._extract_product_media(_product(video_duration=25.75))
        unchanged = untouched.get_info_extractor("Instagram")._extract_product_media(_product())

    assert native["duration"] == 25.75
    assert unchanged["duration"] is None


@pytest.mark.parametrize("kind", ["dynamic", "unknown"])
def test_extractor_does_not_fill_duration_from_nonstatic_mpd(kind: str) -> None:
    with YoutubeDL({"quiet": True}) as ydl:
        install_instagram_duration_fallback(ydl)
        parsed = ydl.get_info_extractor("Instagram")._extract_product_media(
            _product(video_dash_manifest=_manifest(kind=kind)),
        )

    assert parsed["duration"] is None


@pytest.mark.parametrize("manifest", [
    None,
    13.35,
    "",
    "<MPD",
    "<html mediaPresentationDuration=\"PT13S\" />",
    '<MPD xmlns="urn:wrong" mediaPresentationDuration="PT13S" />',
    '<MPD type="static"><Period duration="PT13S" /></MPD>',
    '<MPD type="static"><Period><SegmentTemplate duration="13" /></Period></MPD>',
    '<!DOCTYPE MPD [<!ENTITY duration "PT13S">]><MPD mediaPresentationDuration="&duration;"/>',
    '<!DOCTYPE MPD SYSTEM "https://example.invalid/dtd"><MPD mediaPresentationDuration="PT13S"/>',
    '<!ENTITY duration "PT13S"><MPD mediaPresentationDuration="PT13S"/>',
    '<MPD mediaPresentationDuration="PT13S">' + " " * 1_000_000 + "</MPD>",
])
def test_missing_malformed_untrusted_or_incomplete_manifest_has_no_duration(
    manifest: object,
) -> None:
    assert instagram_mpd_duration(manifest) is None


@pytest.mark.parametrize("duration", [
    "", "P", "PT", "P1DT", "NaN", "PTNaNS", "PTInfinityS", "PT-13S", "-PT13S",
    "PT0S", "13.35", "PT1e3S", "P1Y", "P1M", "PT1.5H", "PT1.5M", "P1.5D",
    "PT" + "9" * 400 + "S",
])
def test_invalid_or_nonpositive_total_duration_is_rejected(duration: str) -> None:
    assert instagram_mpd_duration(_manifest(duration)) is None


@pytest.mark.parametrize("duration,expected", [
    ("PT13.350022S", 13.350022),
    ("PT3M0.001S", 180.001),
    ("PT1H2M3S", 3723),
    ("P1DT1S", 86401),
])
def test_valid_mpd_totals_use_declared_duration(duration: str, expected: float) -> None:
    assert instagram_mpd_duration(_manifest(duration)) == expected


def test_omitted_mpd_type_uses_the_static_default() -> None:
    assert instagram_mpd_duration('<MPD mediaPresentationDuration="PT13S"/>') == 13


@pytest.mark.parametrize("duration", [None, False, True, 0, -1, math.nan, math.inf, "nan", "bad"])
def test_unusable_instagram_duration_cannot_pass_as_a_known_length(duration: object) -> None:
    assert instagram_duration_seconds(duration) is None


@pytest.mark.parametrize("duration,expected,skipped", [
    ("PT180S", 180, False),
    ("PT180.000001S", 181, True),
    ("PT13.350022S", 14, False),
])
def test_fractional_duration_above_automatic_limit_remains_rejected(
    duration: str, expected: int, skipped: bool,
) -> None:
    with YoutubeDL({"quiet": True}) as ydl:
        install_instagram_duration_fallback(ydl)
        parsed = ydl.get_info_extractor("Instagram")._extract_product_media(
            _product(video_dash_manifest=_manifest(duration)),
        )
    meta = _metadata_from_info(_URL, "instagram", parsed)

    assert meta.duration_seconds == expected
    assert bool(explain_video_skip(meta, Settings(max_video_duration_seconds=180))) == skipped


def test_other_platforms_keep_the_existing_integer_duration_behavior() -> None:
    info = {"duration": 13.350022, "url": "https://cdn.example/video.mp4"}
    assert _metadata_from_info(_URL, "instagram", info).duration_seconds == 14
    youtube = _metadata_from_info("https://youtu.be/abcdefghijk", "youtube", info)
    assert youtube.duration_seconds == 13
