from pathlib import Path

from src.cookie_utils import get_cookie_header


def test_get_cookie_header(tmp_path: Path) -> None:
    cookie_file = tmp_path / "bilibili.txt"
    cookie_file.write_text(
        "\n".join([
            "# Netscape HTTP Cookie File",
            ".bilibili.com\tTRUE\t/\tTRUE\t1800000000\tSESSDATA\tabc",
            "#HttpOnly_.bilibili.com\tTRUE\t/\tTRUE\t1800000000\tbili_jct\txyz",
        ]),
        encoding="utf-8",
    )

    assert get_cookie_header(str(cookie_file), "bilibili.com") == "SESSDATA=abc; bili_jct=xyz"


def _write_cookies(tmp_path, lines: list[str]) -> str:
    path = tmp_path / "c.txt"
    path.write_text("# Netscape HTTP Cookie File\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def test_playwright_cookies_keeps_session_cookies_with_zero_expiry(tmp_path) -> None:
    """TikTok exports tt_csrf_token with expiry 0; dropping it breaks the session."""
    from src.cookie_utils import playwright_cookies_from_file

    path = _write_cookies(
        tmp_path,
        [".tiktok.com\tTRUE\t/\tTRUE\t0\ttt_csrf_token\tabc"],
    )

    cookies = playwright_cookies_from_file(path, "tiktok.com")

    assert len(cookies) == 1
    assert cookies[0]["name"] == "tt_csrf_token"
    assert cookies[0]["expires"] == -1


def test_playwright_cookies_normalizes_webkit_microsecond_expiry(tmp_path) -> None:
    """Playwright rejects 13455611319580140 outright."""
    from src.cookie_utils import playwright_cookies_from_file

    path = _write_cookies(
        tmp_path,
        [".tiktok.com\tTRUE\t/\tTRUE\t13455611319580140\t_ttp\tabc"],
    )

    cookies = playwright_cookies_from_file(path, "tiktok.com")

    assert len(cookies) == 1
    assert cookies[0]["expires"] == -1


def test_playwright_cookies_still_drops_genuinely_expired(tmp_path) -> None:
    from src.cookie_utils import playwright_cookies_from_file

    path = _write_cookies(
        tmp_path,
        [
            ".tiktok.com\tTRUE\t/\tTRUE\t100\told\tabc",
            ".tiktok.com\tTRUE\t/\tTRUE\t4102444800\tfresh\tdef",
        ],
    )

    cookies = playwright_cookies_from_file(path, "tiktok.com")

    assert [c["name"] for c in cookies] == ["fresh"]
    assert cookies[0]["expires"] == 4102444800
