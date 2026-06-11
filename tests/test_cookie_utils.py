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
