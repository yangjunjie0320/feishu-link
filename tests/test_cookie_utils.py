from pathlib import Path

from feishu_link.cookie_utils import cookie_header_from_netscape_file


def test_cookie_header_from_netscape_file(tmp_path: Path) -> None:
    cookie_file = tmp_path / "zhihu.txt"
    cookie_file.write_text(
        "\n".join([
            "# Netscape HTTP Cookie File",
            ".zhihu.com\tTRUE\t/\tTRUE\t1800000000\tz_c0\tabc",
            "#HttpOnly_.zhihu.com\tTRUE\t/\tTRUE\t1800000000\tSESSIONID\txyz",
        ]),
        encoding="utf-8",
    )

    assert cookie_header_from_netscape_file(str(cookie_file)) == "z_c0=abc; SESSIONID=xyz"
