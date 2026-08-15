import os
import time
from contextlib import asynccontextmanager
from typing import ClassVar

import pytest

import src.cookie_refresh as cookie_refresh
from src.config import Settings
from src.cookie_refresh import (
    browser_login,
    cookie_is_stale,
    ensure_fresh_cookies,
    force_refresh,
    refresh_cookies,
)
from src.cookie_utils import get_cookie_header


def _playwright_cookie(
    name: str,
    value: str,
    expires: float,
    *,
    domain: str = ".bilibili.com",
) -> dict:
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": "/",
        "expires": expires,
        "secure": True,
        "httpOnly": True,
    }


def test_cookie_is_stale_when_file_missing(tmp_path) -> None:
    assert cookie_is_stale(str(tmp_path / "absent.txt"), "bilibili", 86400) is True


def test_cookie_is_stale_false_for_fresh_cookie(tmp_path) -> None:
    target = tmp_path / "bilibili.txt"
    far_future = time.time() + 30 * 86400
    cookie_refresh.write_netscape(
        [_playwright_cookie("SESSDATA", "fresh", far_future)], str(target)
    )

    assert cookie_is_stale(str(target), "bilibili", 86400) is False


def test_cookie_is_stale_true_when_expiring_soon(tmp_path) -> None:
    target = tmp_path / "bilibili.txt"
    soon = time.time() + 3600
    cookie_refresh.write_netscape([_playwright_cookie("SESSDATA", "stale", soon)], str(target))

    assert cookie_is_stale(str(target), "bilibili", 86400) is True


def test_cookie_is_stale_true_when_required_missing(tmp_path) -> None:
    target = tmp_path / "bilibili.txt"
    far_future = time.time() + 30 * 86400
    cookie_refresh.write_netscape([_playwright_cookie("buvid3", "x", far_future)], str(target))

    assert cookie_is_stale(str(target), "bilibili", 86400) is True


def test_cookie_is_stale_supports_instagram_session(tmp_path) -> None:
    target = tmp_path / "instagram.txt"
    far_future = time.time() + 30 * 86400
    cookie_refresh.write_netscape(
        [
            _playwright_cookie(
                "sessionid",
                "fresh",
                far_future,
                domain=".instagram.com",
            )
        ],
        str(target),
    )

    assert cookie_is_stale(str(target), "instagram", 86400) is False


def test_cookie_is_stale_supports_youtube_session(tmp_path) -> None:
    target = tmp_path / "youtube.txt"
    far_future = time.time() + 30 * 86400
    cookie_refresh.write_netscape(
        [
            _playwright_cookie("SAPISID", "fresh", far_future, domain=".youtube.com"),
            _playwright_cookie(
                "__Secure-3PSID",
                "fresh",
                far_future,
                domain=".youtube.com",
            ),
        ],
        str(target),
    )

    assert cookie_is_stale(str(target), "youtube", 86400) is False


def test_cookie_is_stale_true_when_youtube_anchor_missing(tmp_path) -> None:
    target = tmp_path / "youtube.txt"
    far_future = time.time() + 30 * 86400
    cookie_refresh.write_netscape(
        [_playwright_cookie("SAPISID", "fresh", far_future, domain=".youtube.com")],
        str(target),
    )

    assert cookie_is_stale(str(target), "youtube", 86400) is True


def test_cookie_is_stale_when_file_older_than_max_age(tmp_path) -> None:
    # YouTube rotates the session server-side long before the ~2-year nominal
    # expiry, so file age must be able to trigger a refresh on its own.
    target = tmp_path / "youtube.txt"
    far_future = time.time() + 30 * 86400
    cookie_refresh.write_netscape(
        [
            _playwright_cookie("SAPISID", "fresh", far_future, domain=".youtube.com"),
            _playwright_cookie("__Secure-3PSID", "fresh", far_future, domain=".youtube.com"),
        ],
        str(target),
    )
    old = time.time() - 13 * 3600
    os.utime(target, (old, old))

    assert cookie_is_stale(str(target), "youtube", 86400) is False
    assert cookie_is_stale(str(target), "youtube", 86400, max_age_seconds=0) is False
    assert cookie_is_stale(str(target), "youtube", 86400, max_age_seconds=43200) is True


def test_write_netscape_roundtrips_through_reader(tmp_path) -> None:
    target = tmp_path / "bilibili.txt"
    far_future = time.time() + 30 * 86400
    cookie_refresh.write_netscape(
        [
            _playwright_cookie("SESSDATA", "abc", far_future),
            _playwright_cookie("bili_jct", "def", far_future),
        ],
        str(target),
    )

    header = get_cookie_header(str(target), "bilibili.com")
    assert "SESSDATA=abc" in header
    assert "bili_jct=def" in header


@pytest.mark.asyncio
async def test_refresh_cookies_writes_logged_in_session(monkeypatch, tmp_path) -> None:
    target = tmp_path / "bilibili.txt"
    far_future = time.time() + 30 * 86400
    canned = [
        _playwright_cookie("SESSDATA", "live", far_future),
        {"name": "noise", "value": "n", "domain": ".other.com", "path": "/", "expires": -1},
    ]

    class FakePage:
        async def goto(self, *args, **kwargs):
            return None

    class FakeContext:
        pages: ClassVar = [FakePage()]

        async def cookies(self):
            return canned

    @asynccontextmanager
    async def fake_pc(*args, **kwargs):
        yield FakeContext()

    monkeypatch.setattr(cookie_refresh, "persistent_context", fake_pc)

    ok = await refresh_cookies(
        "bilibili",
        Settings(cookie_refresh_source="browser_profile"),
        target=str(target),
    )

    assert ok is True
    header = get_cookie_header(str(target), "bilibili.com")
    assert "SESSDATA=live" in header
    assert "noise" not in header  # other-domain cookie filtered out


@pytest.mark.asyncio
async def test_refresh_cookies_always_navigates_to_renew_session(
    monkeypatch,
    tmp_path,
) -> None:
    # Refresh only runs when the exported cookie is near expiry, so the site
    # must get a page load to rotate the session even if cookies are present.
    target = tmp_path / "bilibili.txt"
    far_future = time.time() + 30 * 86400
    navigated = False

    class FakePage:
        async def goto(self, *args, **kwargs):
            nonlocal navigated
            navigated = True

    class FakeContext:
        pages: ClassVar = [FakePage()]

        async def cookies(self):
            return [_playwright_cookie("SESSDATA", "live", far_future)]

    @asynccontextmanager
    async def fake_pc(*args, **kwargs):
        yield FakeContext()

    monkeypatch.setattr(cookie_refresh, "persistent_context", fake_pc)

    ok = await refresh_cookies(
        "bilibili",
        Settings(cookie_refresh_source="browser_profile"),
        target=str(target),
    )

    assert ok is True
    assert navigated is True
    assert "SESSDATA=live" in get_cookie_header(str(target), "bilibili.com")


@pytest.mark.asyncio
async def test_refresh_cookies_skips_when_not_logged_in(monkeypatch, tmp_path) -> None:
    target = tmp_path / "bilibili.txt"
    navigated = False

    class FakePage:
        async def goto(self, *args, **kwargs):
            nonlocal navigated
            navigated = True
            return None

    class FakeContext:
        pages: ClassVar = [FakePage()]

        async def cookies(self):
            return [_playwright_cookie("buvid3", "x", time.time() + 86400)]

    @asynccontextmanager
    async def fake_pc(*args, **kwargs):
        yield FakeContext()

    monkeypatch.setattr(cookie_refresh, "persistent_context", fake_pc)

    ok = await refresh_cookies(
        "bilibili",
        Settings(cookie_refresh_source="browser_profile"),
        target=str(target),
    )

    assert ok is False
    assert navigated is True
    assert not target.exists()


@pytest.mark.asyncio
async def test_ensure_fresh_cookies_noop_when_disabled(monkeypatch, tmp_path) -> None:
    called = False

    async def fake_refresh(*args, **kwargs):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(cookie_refresh, "refresh_cookies", fake_refresh)

    settings = Settings(
        cookie_refresh_enabled=False,
        cookie_refresh_platforms=["bilibili"],
        platform_cookie_files={"bilibili": str(tmp_path / "bilibili.txt")},
    )
    await ensure_fresh_cookies("bilibili", settings)

    assert called is False


@pytest.mark.asyncio
async def test_ensure_fresh_cookies_refreshes_when_stale(monkeypatch, tmp_path) -> None:
    cookie_refresh._last_refresh.clear()
    called_with = {}

    async def fake_refresh(platform, settings, *, target=None):
        called_with["platform"] = platform
        called_with["target"] = target
        return True

    monkeypatch.setattr(cookie_refresh, "refresh_cookies", fake_refresh)

    target = tmp_path / "bilibili.txt"  # missing -> stale
    settings = Settings(
        cookie_refresh_enabled=True,
        cookie_refresh_platforms=["bilibili"],
        platform_cookie_files={"bilibili": str(target)},
    )
    await ensure_fresh_cookies("bilibili", settings)

    assert called_with["platform"] == "bilibili"
    assert called_with["target"] == str(target)


@pytest.mark.asyncio
async def test_force_refresh_ignores_nominal_expiry(monkeypatch, tmp_path) -> None:
    cookie_refresh._last_force.clear()
    target = tmp_path / "youtube.txt"
    far_future = time.time() + 30 * 86400
    cookie_refresh.write_netscape(
        [
            _playwright_cookie("SAPISID", "rotated", far_future, domain=".youtube.com"),
            _playwright_cookie("__Secure-3PSID", "rotated", far_future, domain=".youtube.com"),
        ],
        str(target),
    )
    calls: list[str] = []

    async def fake_refresh(platform, settings, *, target=None):
        calls.append(platform)
        return True

    monkeypatch.setattr(cookie_refresh, "refresh_cookies", fake_refresh)

    settings = Settings(
        cookie_refresh_enabled=True,
        cookie_refresh_platforms=["youtube"],
        platform_cookie_files={"youtube": str(target)},
    )

    # Nominally fresh by expiry, yet the reactive path must still refresh.
    assert await force_refresh("youtube", settings) is True
    assert calls == ["youtube"]


@pytest.mark.asyncio
async def test_force_refresh_throttled_by_reactive_cooldown(monkeypatch, tmp_path) -> None:
    cookie_refresh._last_force.clear()
    calls: list[str] = []

    async def fake_refresh(platform, settings, *, target=None):
        calls.append(platform)
        return True

    monkeypatch.setattr(cookie_refresh, "refresh_cookies", fake_refresh)

    settings = Settings(
        cookie_refresh_enabled=True,
        cookie_refresh_platforms=["youtube"],
        cookie_refresh_reactive_cooldown_seconds=60,
        platform_cookie_files={"youtube": str(tmp_path / "youtube.txt")},
    )

    assert await force_refresh("youtube", settings) is True
    assert await force_refresh("youtube", settings) is False
    assert calls == ["youtube"]


@pytest.mark.asyncio
async def test_force_refresh_noop_when_disabled_or_unlisted(monkeypatch, tmp_path) -> None:
    cookie_refresh._last_force.clear()
    called = False

    async def fake_refresh(*args, **kwargs):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(cookie_refresh, "refresh_cookies", fake_refresh)
    cookie_files = {"youtube": str(tmp_path / "youtube.txt")}

    disabled = Settings(
        cookie_refresh_enabled=False,
        cookie_refresh_platforms=["youtube"],
        platform_cookie_files=cookie_files,
    )
    unlisted = Settings(
        cookie_refresh_enabled=True,
        cookie_refresh_platforms=["bilibili"],
        platform_cookie_files=cookie_files,
    )

    assert await force_refresh("youtube", disabled) is False
    assert await force_refresh("youtube", unlisted) is False
    assert called is False


@pytest.mark.asyncio
async def test_browser_login_rejects_unknown_platform() -> None:
    assert await browser_login("douyin", Settings()) is False


def test_tiktok_refresh_profile_registered() -> None:
    profile = cookie_refresh._PROFILES["tiktok"]
    assert profile.cookie_domain == "tiktok.com"
    assert "sessionid" in profile.required_names


@pytest.mark.asyncio
async def test_browser_login_reports_closed_window(monkeypatch, tmp_path, caplog) -> None:
    class FakePage:
        async def goto(self, *args, **kwargs):
            return None

    class FakeContext:
        pages: ClassVar = [FakePage()]

        async def cookies(self):
            raise RuntimeError("Target page, context or browser has been closed")

    @asynccontextmanager
    async def fake_pc(*args, **kwargs):
        yield FakeContext()

    monkeypatch.setattr(cookie_refresh, "persistent_context", fake_pc)

    settings = Settings(platform_cookie_files={"x": str(tmp_path / "x.txt")})
    ok = await browser_login("x", settings)

    assert ok is False
    assert any("closed before session cookies" in r.getMessage() for r in caplog.records)
    assert not any(r.levelname == "ERROR" for r in caplog.records)


@pytest.mark.asyncio
async def test_browser_login_saves_cookies_when_logged_in(monkeypatch, tmp_path) -> None:
    target = tmp_path / "bilibili.txt"
    far_future = time.time() + 30 * 86400

    class FakePage:
        async def goto(self, *args, **kwargs):
            return None

    class FakeContext:
        pages: ClassVar = [FakePage()]

        async def cookies(self):
            return [_playwright_cookie("SESSDATA", "logged-in", far_future)]

    @asynccontextmanager
    async def fake_pc(*args, **kwargs):
        assert kwargs.get("headless") is False
        yield FakeContext()

    monkeypatch.setattr(cookie_refresh, "persistent_context", fake_pc)

    settings = Settings(platform_cookie_files={"bilibili": str(target)})
    ok = await browser_login("bilibili", settings)

    assert ok is True
    assert "SESSDATA=logged-in" in get_cookie_header(str(target), "bilibili.com")


def test_default_cookie_source_is_chrome() -> None:
    assert Settings().cookie_refresh_source == "chrome"


def test_cookiejar_cookie_to_dict_maps_fields() -> None:
    from http.cookiejar import Cookie

    cookie = Cookie(
        version=0,
        name="SESSDATA",
        value="live",
        port=None,
        port_specified=False,
        domain=".bilibili.com",
        domain_specified=True,
        domain_initial_dot=True,
        path="/",
        path_specified=True,
        secure=True,
        expires=1900000000,
        discard=False,
        comment=None,
        comment_url=None,
        rest={"HTTPOnly": None},
    )

    converted = cookie_refresh._cookiejar_cookie_to_dict(cookie)

    assert converted["domain"] == ".bilibili.com"
    assert converted["name"] == "SESSDATA"
    assert converted["value"] == "live"
    assert converted["secure"] is True
    assert converted["expires"] == 1900000000
    assert converted["httpOnly"] is True


@pytest.mark.asyncio
async def test_refresh_from_chrome_writes_platform_cookies(monkeypatch, tmp_path) -> None:
    target = tmp_path / "bilibili.txt"
    far_future = time.time() + 30 * 86400
    extracted = [
        _playwright_cookie("SESSDATA", "from-chrome", far_future),
        _playwright_cookie("noise", "n", far_future, domain=".other.com"),
    ]

    monkeypatch.setattr(
        cookie_refresh, "_extract_chrome_cookies_sync", lambda chrome_profile: extracted
    )

    ok = await refresh_cookies("bilibili", Settings(), target=str(target))

    assert ok is True
    header = get_cookie_header(str(target), "bilibili.com")
    assert "SESSDATA=from-chrome" in header
    assert "noise" not in header


@pytest.mark.asyncio
async def test_refresh_from_chrome_reports_not_logged_in(monkeypatch, tmp_path, caplog) -> None:
    target = tmp_path / "bilibili.txt"
    far_future = time.time() + 30 * 86400

    monkeypatch.setattr(
        cookie_refresh,
        "_extract_chrome_cookies_sync",
        lambda chrome_profile: [_playwright_cookie("buvid3", "anon", far_future)],
    )

    ok = await refresh_cookies("bilibili", Settings(), target=str(target))

    assert ok is False
    assert not target.exists()
    assert any("open Chrome" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_refresh_from_chrome_swallows_extraction_failure(
    monkeypatch, tmp_path, caplog
) -> None:
    target = tmp_path / "bilibili.txt"

    def boom(chrome_profile):
        raise RuntimeError("keychain access denied")

    monkeypatch.setattr(cookie_refresh, "_extract_chrome_cookies_sync", boom)

    ok = await refresh_cookies("bilibili", Settings(), target=str(target))

    assert ok is False
    assert any("Chrome cookie extraction" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_browser_login_saves_instagram_cookies(monkeypatch, tmp_path) -> None:
    target = tmp_path / "instagram.txt"
    far_future = time.time() + 30 * 86400

    class FakePage:
        async def goto(self, *args, **kwargs):
            return None

    class FakeContext:
        pages: ClassVar = [FakePage()]

        async def cookies(self):
            return [
                _playwright_cookie(
                    "sessionid",
                    "logged-in",
                    far_future,
                    domain=".instagram.com",
                )
            ]

    @asynccontextmanager
    async def fake_pc(*args, **kwargs):
        assert kwargs.get("headless") is False
        yield FakeContext()

    monkeypatch.setattr(cookie_refresh, "persistent_context", fake_pc)

    settings = Settings(platform_cookie_files={"instagram": str(target)})
    ok = await browser_login("instagram", settings)

    assert ok is True
    assert "sessionid=logged-in" in get_cookie_header(str(target), "instagram.com")
