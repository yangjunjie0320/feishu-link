import asyncio
import contextlib
import io
import json
import os
import signal
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import Settings
from src.parsers.base import MediaType
from src.parsers.ytdlp import YtDlpMetadataParser, _metadata_from_info


def test_metadata_from_ytdlp_info() -> None:
    meta = _metadata_from_info(
        "https://www.tiktok.com/@u/video/123",
        "tiktok",
        {
            "title": "Short clip",
            "description": "hello",
            "thumbnail": "https://example.com/cover.jpg",
            "duration": 12.3,
            "view_count": 123456,
            "like_count": 7890,
            "comment_count": 123,
            "repost_count": 45,
            "uploader": "creator",
            "webpage_url": "https://www.tiktok.com/@u/video/123",
            "formats": [{
                "url": "https://cdn.example.com/video.mp4?token=secret",
                "format_id": "18",
                "ext": "mp4",
                "filesize": 1234,
            }],
        },
    )

    assert meta.platform == "tiktok"
    assert meta.site_name == "TikTok"
    assert meta.media_type == MediaType.VIDEO
    assert meta.duration_seconds == 12
    assert meta.channel == "creator"
    assert meta.view_count == 123456
    assert meta.like_count == 7890
    assert meta.comment_count == 123
    assert meta.repost_count == 45
    assert meta.download_candidates[0].format_id == "18"


def test_metadata_uses_nested_thumbnail_for_image_posts() -> None:
    meta = _metadata_from_info(
        "https://www.instagram.com/p/abc/",
        "instagram",
        {
            "title": "Instagram Post",
            "description": "caption",
            "entries": [{
                "id": "image-1",
                "thumbnails": [{
                    "url": "https://cdn.example.com/photo.webp?token=secret",
                }],
            }],
        },
    )

    assert meta.cover_url == "https://cdn.example.com/photo.webp?token=secret"


@pytest.mark.asyncio
async def test_ytdlp_worker_receives_temporary_cookie_and_refreshes(monkeypatch, tmp_path) -> None:
    from src.parsers.ytdlp_worker import metadata_options

    original = tmp_path / "cookies.txt"
    original.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    seen = []
    refresh = AsyncMock()
    monkeypatch.setattr("src.parsers.ytdlp.ensure_fresh_cookies", refresh)
    monkeypatch.setattr("src.ytdlp_options.shutil.which", lambda name: "/usr/local/bin/deno")

    async def worker(url, platform, cookie_file):
        seen.append(cookie_file)
        assert Path(cookie_file).is_file()
        assert cookie_file != str(original)
        options = metadata_options(platform, cookie_file)
        assert options["http_headers"]["Referer"] == "https://www.bilibili.com/"
        assert options["js_runtimes"] == {"deno": {"path": "/usr/local/bin/deno"}}
        return {"source_url": url, "platform": platform, "title": "Video",
                "media_type": "video", "duration_seconds": 12,
                "download_candidates": [{"url": "https://cdn.example.com/v.mp4"}]}

    monkeypatch.setattr("src.parsers.ytdlp._run_metadata_worker", worker)
    settings = Settings(cookie_file=str(original))
    meta = await YtDlpMetadataParser(settings).parse("https://www.bilibili.com/video/BV123")
    assert meta.title == "Video"
    assert meta.media_type == MediaType.VIDEO
    assert meta.download_candidates[0].url.endswith("v.mp4")
    refresh.assert_awaited_once_with("bilibili", settings)
    assert not Path(seen[0]).exists()
    assert original.exists()


def test_metadata_options_do_not_enable_impersonation() -> None:
    from src.parsers.ytdlp_worker import metadata_options

    options = metadata_options("tiktok", "")
    assert "impersonate" not in options
    assert "http_headers" not in options
    assert "cookiefile" not in options


@pytest.mark.asyncio
async def test_media_timeout_terminates_worker(monkeypatch) -> None:
    from src.parsers.base import ParserError

    create = asyncio.create_subprocess_exec
    processes = []

    async def substitute(*args, **kwargs):
        process = await create(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr("src.parsers.ytdlp.ensure_fresh_cookies", AsyncMock())
    monkeypatch.setattr("src.parsers.ytdlp.asyncio.create_subprocess_exec", substitute)
    parser = YtDlpMetadataParser(Settings(media_metadata_timeout=0.15))
    with pytest.raises(ParserError, match="time budget exhausted"):
        await parser.parse("https://www.youtube.com/watch?v=abcdefghijk")
    assert processes and processes[0].returncode is not None


@pytest.mark.asyncio
async def test_media_timeout_cleans_child_pipes_after_worker_has_exited(
    monkeypatch, tmp_path,
) -> None:
    from src.parsers.base import ParserError

    create = asyncio.create_subprocess_exec
    killpg = os.killpg
    groups_stopped = []
    processes = []
    child_pid_file = tmp_path / "child.pid"
    script = (
        "import os, pathlib, subprocess, sys; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid)); os._exit(0)"
    )

    async def substitute(*args, **kwargs):
        process = await create(sys.executable, "-c", script, **kwargs)
        processes.append(process)
        while process.returncode is None:
            await asyncio.sleep(0.01)
        return process

    def stop_group(pid, sig):
        groups_stopped.append((pid, sig))
        killpg(pid, sig)

    monkeypatch.setattr("src.parsers.ytdlp.ensure_fresh_cookies", AsyncMock())
    monkeypatch.setattr("src.parsers.ytdlp.asyncio.create_subprocess_exec", substitute)
    monkeypatch.setattr("src.parsers.ytdlp.os.killpg", stop_group)
    parser = YtDlpMetadataParser(Settings(media_metadata_timeout=0.5))
    try:
        with pytest.raises(ParserError, match="time budget exhausted"):
            await parser.parse("https://www.youtube.com/watch?v=abcdefghijk")
        assert child_pid_file.exists()
        assert processes[0].returncode == 0
        assert (processes[0].pid, signal.SIGTERM) in groups_stopped
        assert (processes[0].pid, signal.SIGKILL) in groups_stopped
        # A surviving child keeps communicate() blocked even though its parent
        # is already reaped. EOF proves the descendant released those pipes.
        await asyncio.wait_for(processes[0].communicate(), timeout=1)
    finally:
        for process in processes:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                killpg(process.pid, signal.SIGKILL)
            await asyncio.wait_for(process.communicate(), timeout=1)


def test_worker_keeps_downloadable_video_when_thumbnails_are_null(monkeypatch) -> None:
    import yt_dlp

    from src.parsers import ytdlp_worker

    url = "https://www.tiktok.com/@creator/video/12345"
    info = {
        "id": "12345", "title": "Real video", "duration": 12, "thumbnails": None,
        "formats": [{"url": "https://cdn.example.com/video.mp4", "ext": "mp4"}],
    }
    downloader = MagicMock()
    downloader.__enter__.return_value.extract_info.return_value = info
    monkeypatch.setattr(yt_dlp, "YoutubeDL", lambda options: downloader)
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"url": url, "platform": "tiktok"})))
    monkeypatch.setattr(sys, "stdout", output)

    assert ytdlp_worker.main() == 0
    result = json.loads(output.getvalue())
    assert result["media_type"] == "video"
    assert result["duration_seconds"] == 12
    assert result["download_candidates"][0]["url"] == "https://cdn.example.com/video.mp4"


@pytest.mark.asyncio
async def test_media_slot_queue_is_bounded_by_deadline(monkeypatch) -> None:
    from src.parsers.base import ParserError

    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def worker(url, platform, cookies):
        calls.append(url)
        entered.set()
        await release.wait()
        return {"source_url": url}

    monkeypatch.setattr("src.parsers.ytdlp._run_metadata_worker", worker)
    monkeypatch.setattr("src.parsers.ytdlp.ensure_fresh_cookies", AsyncMock())
    parser = YtDlpMetadataParser(Settings(media_metadata_concurrency=1, media_metadata_timeout=0.1))
    first = asyncio.create_task(parser.parse("https://youtu.be/abcdefghijk"))
    await entered.wait()
    with pytest.raises(ParserError, match="time budget exhausted"):
        await parser.parse("https://youtu.be/lmnopqrstuv")
    with pytest.raises(ParserError):
        await first
    assert len(calls) <= 2
