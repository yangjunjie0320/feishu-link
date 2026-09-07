from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.config import Settings
from src.parsers.base import LinkMetadata, ParserError
from src.parsers.ytdlp import YtDlpMetadataParser

_URL = "https://www.instagram.com/reel/Dbuti2PMeW6/"


def _metadata(url: str = _URL, platform: str = "instagram", **fields: object) -> dict:
    return {
        "source_url": url,
        "platform": platform,
        "title": "A real video caption",
        "media_type": "video",
        "duration_seconds": 13,
        "download_candidates": [{"url": "https://cdn.example/video.mp4", "ext": "mp4"}],
        **fields,
    }


def _settings(tmp_path: Path, **fields: object) -> tuple[Settings, Path]:
    cookie = tmp_path / "instagram.txt"
    cookie.write_text("original login state", encoding="utf-8")
    return Settings(platform_cookie_files={"instagram": str(cookie)}, **fields), cookie


async def test_public_instagram_success_does_not_refresh_or_pass_cookies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, cookie = _settings(tmp_path)
    refresh = AsyncMock()
    worker = AsyncMock(return_value=_metadata())
    monkeypatch.setattr("src.parsers.ytdlp.ensure_fresh_cookies", refresh)
    monkeypatch.setattr("src.parsers.ytdlp._run_metadata_worker", worker)

    parsed = await YtDlpMetadataParser(settings).parse(_URL)

    worker.assert_awaited_once_with(_URL, "instagram", "")
    refresh.assert_not_awaited()
    assert parsed.download_uses_cookies is False
    assert parsed.duration_seconds == 13
    assert parsed.download_candidates
    assert cookie.read_text(encoding="utf-8") == "original login state"


@pytest.mark.parametrize("reason", [
    "This account is private",
    "Login required. Use cookies to access this video",
    "HTTP Error 401: Unauthorized",
    "HTTP Error 403: Forbidden",
])
async def test_explicit_login_failure_uses_one_refreshed_temporary_cookie_copy(
    reason: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, original = _settings(tmp_path)
    refresh = AsyncMock()
    copies: list[Path] = []
    calls: list[str] = []

    async def worker(url: str, platform: str, cookie_file: str) -> dict:
        calls.append(cookie_file)
        if not cookie_file:
            refresh.assert_not_awaited()
            raise ParserError(url, reason)
        refresh.assert_awaited_once_with("instagram", settings)
        temporary = Path(cookie_file)
        copies.append(temporary)
        assert temporary != original
        assert temporary.read_text(encoding="utf-8") == "original login state"
        temporary.write_text("yt-dlp updates its own temporary cookie jar", encoding="utf-8")
        return _metadata()

    monkeypatch.setattr("src.parsers.ytdlp.ensure_fresh_cookies", refresh)
    monkeypatch.setattr("src.parsers.ytdlp._run_metadata_worker", worker)

    parsed = await YtDlpMetadataParser(settings).parse(_URL)

    assert parsed.download_uses_cookies is True
    assert len(calls) == 2
    assert not copies[0].exists()
    assert original.read_text(encoding="utf-8") == "original login state"


@pytest.mark.parametrize("reason", [
    "HTTP Error 429: Too many requests; login required",
    "Requested content is not available, rate-limit reached or login required",
    "HTTP Error 403: captcha required",
    "Login required: challenge checkpoint",
    "This private video was deleted",
    "Private post not found (HTTP Error 404)",
    "Private video is geo-restricted",
    "Private video is not available in your country",
    "Login required after automated requests",
    "HTTP Error 400: Bad request",
    "Unable to extract metadata",
    "media worker returned non-JSON output",
])
async def test_non_authentication_failures_do_not_switch_to_login(
    reason: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, _ = _settings(tmp_path)
    refresh = AsyncMock()
    worker = AsyncMock(side_effect=ParserError(_URL, reason))
    monkeypatch.setattr("src.parsers.ytdlp.ensure_fresh_cookies", refresh)
    monkeypatch.setattr("src.parsers.ytdlp._run_metadata_worker", worker)

    with pytest.raises(ParserError) as raised:
        await YtDlpMetadataParser(settings).parse(_URL)

    assert raised.value.reason == reason
    worker.assert_awaited_once_with(_URL, "instagram", "")
    refresh.assert_not_awaited()


@pytest.mark.parametrize("requires_auth,candidates,expected_calls", [
    (True, [], 2),
    (False, [], 1),
    (True, [{"url": "https://cdn.example/video.mp4"}], 1),
])
async def test_only_explicit_authentication_without_media_triggers_login_fallback(
    requires_auth: bool, candidates: list, expected_calls: int,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, _ = _settings(tmp_path)
    refresh = AsyncMock()
    worker = AsyncMock(side_effect=[
        _metadata(requires_auth=requires_auth, download_candidates=candidates), _metadata(),
    ])
    monkeypatch.setattr("src.parsers.ytdlp.ensure_fresh_cookies", refresh)
    monkeypatch.setattr("src.parsers.ytdlp._run_metadata_worker", worker)

    parsed = await YtDlpMetadataParser(settings).parse(_URL)

    assert worker.await_count == expected_calls
    assert refresh.await_count == expected_calls - 1
    assert parsed.download_uses_cookies is (expected_calls == 2)


async def test_login_fallback_requires_a_configured_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refresh = AsyncMock()
    worker = AsyncMock(side_effect=ParserError(_URL, "Login required"))
    monkeypatch.setattr("src.parsers.ytdlp.ensure_fresh_cookies", refresh)
    monkeypatch.setattr("src.parsers.ytdlp._run_metadata_worker", worker)

    with pytest.raises(ParserError, match="Login required"):
        await YtDlpMetadataParser(Settings()).parse(_URL)

    worker.assert_awaited_once_with(_URL, "instagram", "")
    refresh.assert_not_awaited()


async def test_missing_cookie_file_does_not_repeat_anonymous_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, cookie = _settings(tmp_path)
    cookie.unlink()
    refresh = AsyncMock()
    worker = AsyncMock(side_effect=ParserError(_URL, "Login required"))
    monkeypatch.setattr("src.parsers.ytdlp.ensure_fresh_cookies", refresh)
    monkeypatch.setattr("src.parsers.ytdlp._run_metadata_worker", worker)

    with pytest.raises(ParserError, match="Login required"):
        await YtDlpMetadataParser(settings).parse(_URL)

    worker.assert_awaited_once_with(_URL, "instagram", "")
    refresh.assert_awaited_once_with("instagram", settings)


async def test_authenticated_failure_is_not_retried_and_cleans_up_cookie_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, _ = _settings(tmp_path)
    refresh = AsyncMock()
    copies: list[Path] = []

    async def worker(url: str, platform: str, cookie_file: str) -> dict:
        if not cookie_file:
            raise ParserError(url, "Login required")
        copies.append(Path(cookie_file))
        raise ParserError(url, "HTTP Error 400: bad logged-in session")

    monkeypatch.setattr("src.parsers.ytdlp.ensure_fresh_cookies", refresh)
    monkeypatch.setattr("src.parsers.ytdlp._run_metadata_worker", worker)

    with pytest.raises(ParserError, match="bad logged-in session"):
        await YtDlpMetadataParser(settings).parse(_URL)

    refresh.assert_awaited_once_with("instagram", settings)
    assert len(copies) == 1
    assert not copies[0].exists()


async def test_anonymous_and_login_fallback_share_one_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, _ = _settings(tmp_path, media_metadata_timeout=0.08)
    copies: list[Path] = []
    fallback_cancelled = asyncio.Event()

    async def worker(url: str, platform: str, cookie_file: str) -> dict:
        if not cookie_file:
            await asyncio.sleep(0.04)
            raise ParserError(url, "Private post")
        copies.append(Path(cookie_file))
        try:
            await asyncio.sleep(0.06)
        finally:
            fallback_cancelled.set()
        return _metadata()

    monkeypatch.setattr("src.parsers.ytdlp.ensure_fresh_cookies", AsyncMock())
    monkeypatch.setattr("src.parsers.ytdlp._run_metadata_worker", worker)

    with pytest.raises(ParserError, match="time budget exhausted"):
        await YtDlpMetadataParser(settings).parse(_URL)

    assert fallback_cancelled.is_set()
    assert len(copies) == 1
    assert not copies[0].exists()


async def test_cancellation_cleans_up_authenticated_copy_and_releases_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, _ = _settings(tmp_path, media_metadata_concurrency=1)
    entered = asyncio.Event()
    copies: list[Path] = []

    async def worker(url: str, platform: str, cookie_file: str) -> dict:
        if not cookie_file:
            raise ParserError(url, "Private post")
        copies.append(Path(cookie_file))
        entered.set()
        await asyncio.Event().wait()
        return _metadata()

    monkeypatch.setattr("src.parsers.ytdlp.ensure_fresh_cookies", AsyncMock())
    monkeypatch.setattr("src.parsers.ytdlp._run_metadata_worker", worker)
    parser = YtDlpMetadataParser(settings)
    task = asyncio.create_task(parser.parse(_URL))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(copies) == 1
    assert not copies[0].exists()
    monkeypatch.setattr(
        "src.parsers.ytdlp._run_metadata_worker", AsyncMock(return_value=_metadata()),
    )
    parsed = await asyncio.wait_for(parser.parse(_URL), timeout=1)
    assert parsed.download_uses_cookies is False


@pytest.mark.parametrize("configured", [True, False])
async def test_other_platforms_preserve_the_existing_cookie_behavior(
    configured: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cookie = tmp_path / "bilibili.txt"
    cookie.write_text("existing cookie", encoding="utf-8")
    settings = Settings(platform_cookie_files={"bilibili": str(cookie)} if configured else {})
    url = "https://www.bilibili.com/video/BV1example"
    refresh = AsyncMock()
    copies: list[Path] = []

    async def worker(worker_url: str, platform: str, cookie_file: str) -> dict:
        refresh.assert_awaited_once_with("bilibili", settings)
        assert bool(cookie_file) == configured
        if cookie_file:
            temporary = Path(cookie_file)
            assert temporary.exists() and temporary != cookie
            copies.append(temporary)
        return _metadata(worker_url, platform)

    monkeypatch.setattr("src.parsers.ytdlp.ensure_fresh_cookies", refresh)
    monkeypatch.setattr("src.parsers.ytdlp._run_metadata_worker", worker)

    parsed = await YtDlpMetadataParser(settings).parse(url)

    assert parsed.download_uses_cookies is True
    assert all(not temporary.exists() for temporary in copies)
    assert LinkMetadata(source_url=url).download_uses_cookies is True
