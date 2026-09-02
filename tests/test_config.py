from pathlib import Path

import pytest
from pydantic import ValidationError

from src.bibi_client import BibiClient
from src.config import Settings


def test_cookie_file_for_platform_prefers_explicit_mapping(tmp_path: Path) -> None:
    unified = tmp_path / "cookies.txt"
    platform_cookie = tmp_path / "x.txt"
    unified.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    platform_cookie.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

    settings = Settings(
        cookie_file=str(unified),
        platform_cookie_files={"x": str(platform_cookie)},
    )

    assert settings.cookie_file_for_platform("x") == str(platform_cookie)


def test_cookie_file_for_platform_uses_unified_cookie_file(tmp_path: Path) -> None:
    unified = tmp_path / "cookies.txt"
    unified.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

    settings = Settings(cookie_file=str(unified))

    assert settings.cookie_file_for_platform("youtube") == str(unified)


def test_cookie_file_for_platform_prefers_conventional_platform_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    unified = tmp_path / "cookies.txt"
    platform_cookie = tmp_path / "cookies" / "bilibili.txt"
    unified.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    platform_cookie.parent.mkdir()
    platform_cookie.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

    settings = Settings(cookie_file=str(unified))

    assert settings.cookie_file_for_platform("bilibili") == str(Path("cookies/bilibili.txt"))


def test_cookie_refresh_defaults_to_bilibili() -> None:
    settings = Settings()

    assert settings.cookie_refresh_enabled is True
    assert settings.cookie_refresh_platforms == ["bilibili"]
    assert settings.cookie_refresh_profile_dir == "browser-data/cookies"


def test_bibi_client_uses_bibigpt_platform_cookie_file(tmp_path: Path) -> None:
    cookie_file = tmp_path / "bibigpt.txt"
    cookie_file.write_text(
        "\n".join(
            [
                "# Netscape HTTP Cookie File",
                ".aitodo.co\tTRUE\t/\tTRUE\t1800000000\tsession\tabc",
            ]
        ),
        encoding="utf-8",
    )
    settings = Settings(
        cookie_file="",
        platform_cookie_files={"bibigpt": str(cookie_file)},
    )

    client = BibiClient(settings)

    assert client._headers["Cookie"] == "session=abc"


def test_bibigpt_access_mode_rejects_api() -> None:
    with pytest.raises(ValidationError, match="must be 'web' or 'browser'"):
        Settings(bibigpt_access_mode="api")


def test_bibigpt_defaults() -> None:
    settings = Settings()

    assert settings.bibigpt_base_url == "https://aitodo.co/zh"
    assert settings.bibigpt_model == "openai/gpt-5.5"


def test_deepseek_model_defaults_to_v4_flash_and_allows_override() -> None:
    assert Settings().deepseek_model == "deepseek-v4-flash"
    assert Settings(deepseek_model="deepseek-v4-pro").deepseek_model == "deepseek-v4-pro"


def test_bitable_and_report_defaults() -> None:
    settings = Settings()

    assert settings.bitable_enabled is False
    assert settings.bitable_app_token == ""
    assert settings.bitable_table_id == ""
    assert settings.report_enabled is False
    assert settings.report_time == "22:00"
    assert settings.report_timezone == "Asia/Shanghai"
    assert settings.report_chat_id == ""


def test_report_time_rejects_invalid_format() -> None:
    with pytest.raises(ValidationError, match="report_time"):
        Settings(report_time="9:60")
    with pytest.raises(ValidationError, match="report_time"):
        Settings(report_time="2200")


def test_effective_report_chat_ids_falls_back_to_archive() -> None:
    assert Settings(archive_chat_id="oc_a").effective_report_chat_ids() == ["oc_a"]
    assert Settings(archive_chat_id="oc_a", report_chat_id="oc_r").effective_report_chat_ids() == [
        "oc_r"
    ]
    assert Settings().effective_report_chat_ids() == []


def test_effective_report_chat_ids_merges_and_dedups() -> None:
    settings = Settings(
        archive_chat_id="oc_a",
        report_chat_id="oc_r",
        report_chat_ids=["oc_x", " oc_r ", "oc_x", ""],
    )
    assert settings.effective_report_chat_ids() == ["oc_r", "oc_x"]
    # report_chat_ids alone is enough; the archive chat is not added on top.
    assert Settings(
        archive_chat_id="oc_a", report_chat_ids=["oc_x"]
    ).effective_report_chat_ids() == ["oc_x"]


def test_comment_fetch_timeout_is_platform_specific_for_tiktok() -> None:
    settings = Settings()
    assert settings.comment_fetch_timeout_for("tiktok") == settings.tiktok_comment_fetch_timeout
    assert settings.comment_fetch_timeout_for("TikTok") == settings.tiktok_comment_fetch_timeout
    assert settings.comment_fetch_timeout_for("youtube") == settings.comment_fetch_timeout
    assert settings.comment_fetch_timeout_for("bilibili") == settings.comment_fetch_timeout


def test_comment_fetch_timeout_for_tiktok_falls_back_when_disabled() -> None:
    settings = Settings(tiktok_comment_fetch_timeout=0)
    assert settings.comment_fetch_timeout_for("tiktok") == settings.comment_fetch_timeout
