from pathlib import Path

from src.cookie_utils import get_cookie_header


def test_get_cookie_header(tmp_path: Path) -> None:
    cookie_file = tmp_path / "zhihu.txt"
    cookie_file.write_text(
        "\n".join([
            "# Netscape HTTP Cookie File",
            ".zhihu.com\tTRUE\t/\tTRUE\t1800000000\tz_c0\tabc",
            "#HttpOnly_.zhihu.com\tTRUE\t/\tTRUE\t1800000000\tSESSIONID\txyz",
        ]),
        encoding="utf-8",
    )

    assert get_cookie_header(str(cookie_file), "zhihu.com") == "z_c0=abc; SESSIONID=xyz"
