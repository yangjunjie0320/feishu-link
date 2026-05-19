from pathlib import Path

from feishu_link.config import Settings
from feishu_link.media_downloader import (
    _YTDLP_VIDEO_FORMAT,
    _file_name,
    _strip_symbol_characters,
    explain_video_skip,
)
from feishu_link.parsers.base import DownloadCandidate, LinkMetadata, MediaType


def _video_meta(**kwargs: object) -> LinkMetadata:
    data = {
        "source_url": "https://example.com/video",
        "platform": "youtube",
        "media_type": MediaType.VIDEO,
        "duration_seconds": 30,
        "download_candidates": [DownloadCandidate(url="https://cdn.example.com/v.mp4")],
    }
    data.update(kwargs)
    return LinkMetadata(**data)


def test_skip_unknown_duration(settings: Settings) -> None:
    meta = _video_meta(duration_seconds=None)

    assert explain_video_skip(meta, settings) == "video duration unknown"


def test_skip_over_duration_limit(settings: Settings) -> None:
    meta = _video_meta(duration_seconds=181)

    assert "duration exceeds limit" in (explain_video_skip(meta, settings) or "")


def test_video_at_duration_limit_can_be_attempted(settings: Settings) -> None:
    meta = _video_meta(duration_seconds=180)

    assert explain_video_skip(meta, settings) is None


def test_skip_without_download_candidate(settings: Settings) -> None:
    meta = _video_meta(download_candidates=[])

    assert explain_video_skip(meta, settings) == "no download candidate"


def test_video_can_be_attempted(settings: Settings) -> None:
    assert explain_video_skip(_video_meta(), settings) is None


def test_ytdlp_format_supports_split_audio_video() -> None:
    assert "bestvideo" in _YTDLP_VIDEO_FORMAT
    assert "bestaudio" in _YTDLP_VIDEO_FORMAT
    assert "best[ext=mp4]" in _YTDLP_VIDEO_FORMAT


def test_file_name_strips_symbol_characters() -> None:
    source = "video-" + chr(0x1F600) + ".mp4"
    assert _file_name(Path(source)) == "video-.mp4"


def test_strip_symbol_characters() -> None:
    assert _strip_symbol_characters("a" + chr(0x1F3C6) + "b") == "ab"
