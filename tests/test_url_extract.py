import json

import pytest

from feishu_link.config import Settings
from feishu_link.url_extract import extract_urls


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
