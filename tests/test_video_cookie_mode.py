from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src import media_downloader
from src.config import Settings
from src.dispatch import Dispatcher
from src.parsers.base import DownloadCandidate, LinkMetadata, MediaType

INSTAGRAM = "https://www.instagram.com/reel/Dbuti2PMeW6/"
COOKIE_CONTENT = (
    "# Netscape HTTP Cookie File\n"
    ".instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\ttest-session\n"
)


def video_metadata(*, uses_cookies: bool) -> LinkMetadata:
    return LinkMetadata(
        source_url=INSTAGRAM,
        canonical_url=INSTAGRAM,
        title="Public Instagram video",
        platform="instagram",
        media_type=MediaType.VIDEO,
        duration_seconds=13,
        download_candidates=[DownloadCandidate("https://cdn.example.test/video.mp4")],
        download_uses_cookies=uses_cookies,
    )


@dataclass
class CookieModeHarness:
    settings: Settings
    original_cookie: Path
    temp_root: Path
    ensure_fresh: AsyncMock
    force_refresh: AsyncMock
    temporary_cookie: MagicMock
    attempts: list[dict[str, Any]] = field(default_factory=list)
    copied_cookie_contents: list[str] = field(default_factory=list)
    warn_cookie_invalid: bool = False
    failure_message: str = ""
    failures_remaining: int = 0


@pytest.fixture
def cookie_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> CookieModeHarness:
    original_cookie = tmp_path / "instagram.txt"
    original_cookie.write_text(COOKIE_CONTENT)
    temp_root = tmp_path / "videos"
    settings = Settings(
        platform_cookie_files={"instagram": str(original_cookie)},
        video_temp_dir=str(temp_root),
    )
    ensure_fresh = AsyncMock()
    force_refresh = AsyncMock(return_value=True)
    temporary_cookie = MagicMock(wraps=media_downloader.temporary_cookie_file)
    harness = CookieModeHarness(settings, original_cookie, temp_root, ensure_fresh,
                                force_refresh, temporary_cookie)

    class FakeYoutubeDL:
        def __init__(self, options: dict[str, Any]) -> None:
            self.options = options

        def __enter__(self) -> FakeYoutubeDL:
            return self

        def __exit__(
            self, exc_type: type[BaseException] | None, exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> bool:
            return False

        def download(self, urls: list[str]) -> None:
            assert urls == [INSTAGRAM]
            harness.attempts.append(self.options)
            if cookie_file := self.options.get("cookiefile"):
                assert Path(cookie_file) != original_cookie
                harness.copied_cookie_contents.append(Path(cookie_file).read_text())
            if harness.warn_cookie_invalid:
                self.options["logger"].warning("The provided account cookies are no longer valid")
            if harness.failures_remaining:
                harness.failures_remaining -= 1
                raise RuntimeError(harness.failure_message)
            output = Path(self.options["outtmpl"]).parent / "video.mp4"
            output.write_bytes(b"fake downloaded video")

    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    monkeypatch.setattr(media_downloader, "ensure_fresh_cookies", ensure_fresh)
    monkeypatch.setattr(media_downloader, "force_refresh", force_refresh)
    monkeypatch.setattr(media_downloader, "temporary_cookie_file", temporary_cookie)
    monkeypatch.setattr("src.ytdlp_options.shutil.which", lambda name: None)
    monkeypatch.setattr(
        media_downloader, "_make_feishu_mp4",
        lambda path, temp_dir, duration_seconds, max_file_mb: path,
    )
    monkeypatch.setattr(media_downloader, "_probe_duration_ms", lambda path: 13000)
    return harness


@pytest.mark.parametrize("warn_cookie_invalid", [False, True])
async def test_anonymous_download_does_not_attach_or_refresh_configured_cookies(
    cookie_mode: CookieModeHarness, warn_cookie_invalid: bool,
) -> None:
    cookie_mode.warn_cookie_invalid = warn_cookie_invalid
    assert cookie_mode.settings.cookie_file_for_platform("instagram") == str(
        cookie_mode.original_cookie
    )

    video = await media_downloader.download_video(
        video_metadata(uses_cookies=False), cookie_mode.settings,
    )

    try:
        assert video.duration_ms == 13000
        assert video.path.exists()
        assert len(cookie_mode.attempts) == 1
        assert "cookiefile" not in cookie_mode.attempts[0]
        cookie_mode.ensure_fresh.assert_not_awaited()
        cookie_mode.force_refresh.assert_not_awaited()
        cookie_mode.temporary_cookie.assert_not_called()
        assert cookie_mode.original_cookie.read_text() == COOKIE_CONTENT
    finally:
        video.cleanup()
    assert not list(cookie_mode.temp_root.iterdir())


@pytest.mark.parametrize("signal", ["warning", "exception"])
async def test_anonymous_failure_never_switches_to_configured_cookie_identity(
    cookie_mode: CookieModeHarness, signal: str,
) -> None:
    cookie_mode.warn_cookie_invalid = signal == "warning"
    cookie_mode.failure_message = (
        "Unable to download video data" if signal == "warning"
        else "Sign in to confirm you're not a bot"
    )
    cookie_mode.failures_remaining = 1

    with pytest.raises(media_downloader.VideoDownloadError, match=cookie_mode.failure_message):
        await media_downloader.download_video(
            video_metadata(uses_cookies=False), cookie_mode.settings,
        )

    assert len(cookie_mode.attempts) == 1
    assert "cookiefile" not in cookie_mode.attempts[0]
    cookie_mode.ensure_fresh.assert_not_awaited()
    cookie_mode.force_refresh.assert_not_awaited()
    cookie_mode.temporary_cookie.assert_not_called()
    assert cookie_mode.original_cookie.read_text() == COOKIE_CONTENT
    assert not list(cookie_mode.temp_root.iterdir())


async def test_configured_download_keeps_cookie_refresh_and_temporary_cookie_copy(
    cookie_mode: CookieModeHarness,
) -> None:
    video = await media_downloader.download_video(
        video_metadata(uses_cookies=True), cookie_mode.settings,
    )

    try:
        cookie_mode.ensure_fresh.assert_awaited_once_with("instagram", cookie_mode.settings)
        cookie_mode.force_refresh.assert_not_awaited()
        cookie_mode.temporary_cookie.assert_called_once_with(str(cookie_mode.original_cookie))
        assert len(cookie_mode.attempts) == 1
        assert cookie_mode.copied_cookie_contents == [COOKIE_CONTENT]
        assert not Path(cookie_mode.attempts[0]["cookiefile"]).exists()
        assert cookie_mode.original_cookie.read_text() == COOKIE_CONTENT
    finally:
        video.cleanup()


@pytest.mark.parametrize("signal", ["warning", "exception"])
async def test_configured_download_retains_one_reactive_refresh_and_retry(
    cookie_mode: CookieModeHarness, signal: str,
) -> None:
    cookie_mode.warn_cookie_invalid = signal == "warning"
    cookie_mode.failure_message = (
        "Unable to download video data" if signal == "warning"
        else "Sign in to confirm you're not a bot"
    )
    cookie_mode.failures_remaining = 1

    video = await media_downloader.download_video(
        video_metadata(uses_cookies=True), cookie_mode.settings,
    )

    try:
        cookie_mode.ensure_fresh.assert_awaited_once_with("instagram", cookie_mode.settings)
        cookie_mode.force_refresh.assert_awaited_once_with("instagram", cookie_mode.settings)
        assert len(cookie_mode.attempts) == 2
        assert cookie_mode.temporary_cookie.call_count == 2
        assert cookie_mode.copied_cookie_contents == [COOKIE_CONTENT, COOKIE_CONTENT]
        for attempt in cookie_mode.attempts:
            assert not Path(attempt["cookiefile"]).exists()
        assert cookie_mode.attempts[0]["logger"] is not cookie_mode.attempts[1]["logger"]
        assert cookie_mode.original_cookie.read_text() == COOKIE_CONTENT
    finally:
        video.cleanup()


@pytest.mark.parametrize("media_uses_cookies", [False, True])
async def test_prepare_video_copies_resolved_cookie_mode_without_mutating_card_metadata(
    media_uses_cookies: bool,
) -> None:
    card = video_metadata(uses_cookies=not media_uses_cookies)
    card.title = "Original card caption"
    card.download_candidates = []
    media = video_metadata(uses_cookies=media_uses_cookies)
    async with httpx.AsyncClient() as client:
        dispatcher = Dispatcher(Settings(), client)
        extractor = AsyncMock(return_value=media)
        dispatcher._ytdlp.parse = extractor

        prepared = await dispatcher.prepare_video(card)

    extractor.assert_awaited_once_with(INSTAGRAM)
    assert prepared is not card
    assert prepared.download_uses_cookies is media_uses_cookies
    assert card.download_uses_cookies is not media_uses_cookies
    assert prepared.title == card.title == "Original card caption"
    assert prepared.download_candidates
    assert card.download_candidates == []
