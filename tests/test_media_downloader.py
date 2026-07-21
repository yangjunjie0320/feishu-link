from pathlib import Path
from types import SimpleNamespace

import pytest

import src.media_downloader as media_downloader
from src.config import Settings
from src.media_downloader import (
    _YTDLP_VIDEO_FORMAT,
    _file_name,
    _format_for_platform,
    _strip_symbol_characters,
    _target_transcode_bitrates,
    _transcode_timeout_seconds,
    explain_video_skip,
)
from src.parsers.base import DownloadCandidate, LinkMetadata, MediaType


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


def test_skip_unknown_duration_when_duration_limit_enforced(settings: Settings) -> None:
    meta = _video_meta(duration_seconds=None)

    assert explain_video_skip(meta, settings) == "video duration unknown"


def test_unknown_duration_can_be_attempted_without_duration_limit(settings: Settings) -> None:
    meta = _video_meta(duration_seconds=None)

    assert explain_video_skip(meta, settings, enforce_duration_limit=False) is None


def test_skip_over_duration_limit_when_enforced(settings: Settings) -> None:
    meta = _video_meta(duration_seconds=181)

    assert "duration exceeds limit" in (explain_video_skip(meta, settings) or "")


def test_over_duration_limit_can_be_attempted_without_duration_limit(settings: Settings) -> None:
    meta = _video_meta(duration_seconds=988)

    assert explain_video_skip(meta, settings, enforce_duration_limit=False) is None


def test_video_at_duration_limit_can_be_attempted(settings: Settings) -> None:
    meta = _video_meta(duration_seconds=180)

    assert explain_video_skip(meta, settings) is None


def test_skip_without_download_candidate(settings: Settings) -> None:
    meta = _video_meta(download_candidates=[])

    assert explain_video_skip(meta, settings) == "no download candidate"


def test_large_known_candidates_can_still_be_attempted_for_transcoding(
    settings: Settings,
) -> None:
    meta = _video_meta(
        download_candidates=[
            DownloadCandidate(url="https://cdn.example.com/large.mp4", filesize=31 * 1024 * 1024)
        ]
    )

    assert explain_video_skip(meta, settings) is None


def test_does_not_skip_file_limit_when_smaller_candidate_known(settings: Settings) -> None:
    meta = _video_meta(
        download_candidates=[
            DownloadCandidate(url="https://cdn.example.com/small.mp4", filesize=10 * 1024 * 1024),
            DownloadCandidate(url="https://cdn.example.com/large.mp4", filesize=31 * 1024 * 1024),
        ]
    )

    assert explain_video_skip(meta, settings) is None


def test_video_can_be_attempted(settings: Settings) -> None:
    assert explain_video_skip(_video_meta(), settings) is None


def test_ytdlp_format_prefers_low_quality_video() -> None:
    assert "worst[ext=mp4]" in _YTDLP_VIDEO_FORMAT
    assert "worstvideo" in _YTDLP_VIDEO_FORMAT
    assert "worstaudio" in _YTDLP_VIDEO_FORMAT


def test_tiktok_format_skips_watermarked_download_formats() -> None:
    format_selector = _format_for_platform("tiktok")

    assert "[format_id!=download]" in format_selector
    assert "[format_id!=download_addr]" in format_selector
    assert format_selector.endswith(_YTDLP_VIDEO_FORMAT)
    assert _format_for_platform("youtube") == _YTDLP_VIDEO_FORMAT


@pytest.mark.asyncio
async def test_download_video_passes_ytdlp_runtime_and_bilibili_headers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured_options = {}

    class FakeYoutubeDL:
        def __init__(self, options):
            captured_options.update(options)
            self._options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def download(self, urls):
            output = Path(self._options["outtmpl"]).parent / "video.mp4"
            output.write_bytes(b"fake mp4")

    monkeypatch.setitem(
        __import__("sys").modules,
        "yt_dlp",
        SimpleNamespace(YoutubeDL=FakeYoutubeDL),
    )
    monkeypatch.setattr(
        "src.ytdlp_options.shutil.which",
        lambda name: "/usr/local/bin/deno" if name == "deno" else None,
    )
    monkeypatch.setattr(
        media_downloader,
        "_make_feishu_mp4",
        lambda path, temp_dir, duration_seconds, max_file_mb: path,
    )
    monkeypatch.setattr(media_downloader, "_probe_duration_ms", lambda path: 1234)

    meta = _video_meta(
        source_url="https://www.bilibili.com/video/BV123",
        platform="bilibili",
    )
    settings = Settings(cookie_file="", video_temp_dir=str(tmp_path))

    video = await media_downloader.download_video(meta, settings)

    try:
        assert video.duration_ms == 1234
        assert captured_options["http_headers"]["Referer"] == "https://www.bilibili.com/"
        assert captured_options["js_runtimes"] == {"deno": {"path": "/usr/local/bin/deno"}}
    finally:
        video.cleanup()


@pytest.mark.asyncio
async def test_download_video_ensures_fresh_cookies_before_download(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    refreshed: list[str] = []

    async def fake_ensure(platform, settings):
        refreshed.append(platform)

    monkeypatch.setattr(media_downloader, "ensure_fresh_cookies", fake_ensure)

    class FakeYoutubeDL:
        def __init__(self, options):
            self._options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def download(self, urls):
            output = Path(self._options["outtmpl"]).parent / "video.mp4"
            output.write_bytes(b"fake mp4")

    monkeypatch.setitem(
        __import__("sys").modules,
        "yt_dlp",
        SimpleNamespace(YoutubeDL=FakeYoutubeDL),
    )
    monkeypatch.setattr(
        "src.ytdlp_options.shutil.which",
        lambda name: "/usr/local/bin/deno" if name == "deno" else None,
    )
    monkeypatch.setattr(
        media_downloader,
        "_make_feishu_mp4",
        lambda path, temp_dir, duration_seconds, max_file_mb: path,
    )
    monkeypatch.setattr(media_downloader, "_probe_duration_ms", lambda path: 1234)

    meta = _video_meta(
        source_url="https://www.bilibili.com/video/BV123",
        platform="bilibili",
    )
    settings = Settings(cookie_file="", video_temp_dir=str(tmp_path))

    video = await media_downloader.download_video(meta, settings)
    try:
        assert refreshed == ["bilibili"]
    finally:
        video.cleanup()


@pytest.mark.asyncio
async def test_download_video_refreshes_and_retries_on_cookie_invalidation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    attempts: list[dict[str, object]] = []

    class FakeYoutubeDL:
        def __init__(self, options):
            self._options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def download(self, urls):
            attempts.append(self._options)
            if len(attempts) == 1:
                # The invalidation notice arrives via logger.warning, not the
                # raised exception, mirroring yt-dlp's real behavior.
                self._options["logger"].warning(
                    "The provided YouTube account cookies are no longer valid"
                )
                raise RuntimeError("Unable to download video data")
            output = Path(self._options["outtmpl"]).parent / "video.mp4"
            output.write_bytes(b"fake mp4")

    monkeypatch.setitem(
        __import__("sys").modules,
        "yt_dlp",
        SimpleNamespace(YoutubeDL=FakeYoutubeDL),
    )
    monkeypatch.setattr("src.ytdlp_options.shutil.which", lambda name: None)
    monkeypatch.setattr(
        media_downloader,
        "_make_feishu_mp4",
        lambda path, temp_dir, duration_seconds, max_file_mb: path,
    )
    monkeypatch.setattr(media_downloader, "_probe_duration_ms", lambda path: 1234)

    refreshed: list[str] = []

    async def fake_force_refresh(platform, settings):
        refreshed.append(platform)
        return True

    monkeypatch.setattr(media_downloader, "force_refresh", fake_force_refresh)

    meta = _video_meta(source_url="https://www.youtube.com/watch?v=abc")
    settings = Settings(cookie_file="", video_temp_dir=str(tmp_path))

    video = await media_downloader.download_video(meta, settings)
    try:
        assert refreshed == ["youtube"]
        assert len(attempts) == 2
        assert attempts[0]["logger"] is not attempts[1]["logger"]
    finally:
        video.cleanup()


@pytest.mark.asyncio
async def test_download_video_keeps_failure_when_refresh_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    attempts: list[dict[str, object]] = []

    class FakeYoutubeDL:
        def __init__(self, options):
            self._options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def download(self, urls):
            attempts.append(self._options)
            raise RuntimeError("Sign in to confirm you're not a bot")

    monkeypatch.setitem(
        __import__("sys").modules,
        "yt_dlp",
        SimpleNamespace(YoutubeDL=FakeYoutubeDL),
    )
    monkeypatch.setattr("src.ytdlp_options.shutil.which", lambda name: None)

    async def fake_force_refresh(platform, settings):
        return False

    monkeypatch.setattr(media_downloader, "force_refresh", fake_force_refresh)

    meta = _video_meta(source_url="https://www.youtube.com/watch?v=abc")
    settings = Settings(cookie_file="", video_temp_dir=str(tmp_path))

    with pytest.raises(media_downloader.VideoDownloadError, match="not a bot"):
        await media_downloader.download_video(meta, settings)

    assert len(attempts) == 1


def test_file_name_strips_symbol_characters() -> None:
    source = "video-" + chr(0x1F600) + ".mp4"
    assert _file_name(Path(source)) == "video-.mp4"


def test_strip_symbol_characters() -> None:
    assert _strip_symbol_characters("a" + chr(0x1F3C6) + "b") == "ab"


def test_transcode_timeout_scales_for_long_manual_downloads() -> None:
    assert _transcode_timeout_seconds(988) == 2036


def test_target_transcode_bitrates_fit_long_video_limit() -> None:
    assert _target_transcode_bitrates(988, 30) == (153, 64)


def test_target_transcode_bitrates_cap_short_video_bitrate() -> None:
    assert _target_transcode_bitrates(30, 30) == (1200, 64)


def test_target_transcode_bitrates_need_duration() -> None:
    assert _target_transcode_bitrates(None, 30) is None
