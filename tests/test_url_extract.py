import json

import pytest

from src.config import Settings
from src.url_extract import extract_prompt, extract_urls, is_manual_download_command


@pytest.fixture
def s() -> Settings:
    return Settings()


def test_plain_text_url(s: Settings) -> None:
    content = json.dumps({"text": "Check https://example.com out"})
    assert extract_urls("text", content, s) == ["https://example.com"]


def test_multiple_urls(s: Settings) -> None:
    content = json.dumps({"text": "a https://foo.com b https://bar.com"})
    assert extract_urls("text", content, s) == ["https://foo.com", "https://bar.com"]


def test_deduplication(s: Settings) -> None:
    content = json.dumps({"text": "https://foo.com https://foo.com"})
    assert extract_urls("text", content, s) == ["https://foo.com"]


def test_trailing_punctuation_stripped(s: Settings) -> None:
    content = json.dumps({"text": "see https://example.com."})
    urls = extract_urls("text", content, s)
    assert urls == ["https://example.com"]


def test_blacklist(s: Settings) -> None:
    s2 = Settings(link_blacklist=["example\\.com"])
    content = json.dumps({"text": "https://example.com"})
    assert extract_urls("text", content, s2) == []


def test_allowlist_excludes_unknown_domains() -> None:
    settings = Settings(link_allowlist=["youtube\\.com", "youtu\\.be"])
    content = json.dumps({
        "text": "https://example.com https://www.youtube.com/watch?v=abc"
    })
    assert extract_urls("text", content, settings) == [
        "https://www.youtube.com/watch?v=abc"
    ]


def test_blacklist_overrides_allowlist() -> None:
    settings = Settings(
        link_allowlist=["example\\.com"],
        link_blacklist=["example\\.com/private"],
    )
    content = json.dumps({
        "text": "https://example.com/public https://example.com/private"
    })
    assert extract_urls("text", content, settings) == ["https://example.com/public"]


def test_no_url(s: Settings) -> None:
    content = json.dumps({"text": "hello world"})
    assert extract_urls("text", content, s) == []


def test_feishu_internal_url_skipped(s: Settings) -> None:
    content = json.dumps({"text": "https://open.feishu.cn/abc https://example.com"})
    assert extract_urls("text", content, s) == ["https://example.com"]


def test_youtube_url(s: Settings) -> None:
    content = json.dumps({"text": "watch https://youtu.be/dQw4w9WgXcQ"})
    assert extract_urls("text", content, s) == ["https://youtu.be/dQw4w9WgXcQ"]


def test_url_tightly_followed_by_feishu_mention_is_trimmed(s: Settings) -> None:
    content = json.dumps({
        "text": "https://www.instagram.com/p/DYkarnvCH-O/?igsh=abc==@_user_1"
    })

    assert extract_urls("text", content, s) == [
        "https://www.instagram.com/p/DYkarnvCH-O/?igsh=abc=="
    ]


def test_extract_prompt_removes_video_url_and_mention() -> None:
    url = "https://youtu.be/dQw4w9WgXcQ"
    content = json.dumps({"text": f"@_user_1 {url} summarize in Chinese"})

    assert extract_prompt("text", content, url) == "summarize in Chinese"


def test_extract_prompt_returns_none_when_only_url_and_mention() -> None:
    url = "https://youtu.be/dQw4w9WgXcQ"
    content = json.dumps({"text": f"@_user_1 {url}"})

    assert extract_prompt("text", content, url) is None


def test_manual_download_command_detects_download_after_mention() -> None:
    content = json.dumps({"text": "@_user_1 下载 https://youtu.be/dQw4w9WgXcQ"})

    assert is_manual_download_command("text", content) is True


def test_manual_download_command_supports_colon_separator() -> None:
    content = json.dumps({"text": "下载\uff1ahttps://youtu.be/dQw4w9WgXcQ"})

    assert is_manual_download_command("text", content) is True


def test_manual_download_command_requires_download_prefix() -> None:
    content = json.dumps({"text": "@_user_1 请下载 https://youtu.be/dQw4w9WgXcQ"})

    assert is_manual_download_command("text", content) is False


def test_manual_download_command_requires_url_after_download() -> None:
    content = json.dumps({"text": "@_user_1 下载"})

    assert is_manual_download_command("text", content) is False


def test_decode_plain_text_strips_mention_placeholders() -> None:
    from src.url_extract import decode_plain_text

    raw = json.dumps({"text": "@_user_1 讲得不错 "})
    assert decode_plain_text("text", raw) == "讲得不错"


def test_decode_plain_text_empty_for_whitespace_only() -> None:
    from src.url_extract import decode_plain_text

    assert decode_plain_text("text", json.dumps({"text": " @_user_1 "})) == ""
