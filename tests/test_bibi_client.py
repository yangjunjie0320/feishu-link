import json

import httpx
import pytest
import respx

from src.bibi_client import (
    AuthenticationError,
    BibiAPIError,
    BibiClient,
    TranscriptUnavailableError,
)
from src.config import Settings


@respx.mock
async def test_bibi_client_routes_locale_base_url_to_origin_web(tmp_path) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "\n".join([
            "# Netscape HTTP Cookie File",
            ".aitodo.co\tTRUE\t/\tFALSE\t2147483647\tsession\tabc123",
            ".bibigpt.co\tTRUE\t/\tFALSE\t2147483647\tsession\twrong",
        ]),
        encoding="utf-8",
    )
    route = respx.post(
        "https://aitodo.co/api/trpc/video.summaryBySetting?batch=1"
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "result": {
                        "data": {
                            "json": {
                                "summary": "- Point",
                                "fromCache": False,
                            }
                        }
                    }
                }
            ],
        )
    )
    settings = Settings(
        bibigpt_base_url="https://aitodo.co/zh",
        cookie_file=str(cookie_file),
    )

    result = await BibiClient(settings).summarize("https://youtu.be/abc123")

    request = route.calls.last.request
    body = json.loads(request.content)
    prompt_config = body["0"]["json"]["promptConfig"]

    assert result.content == "- Point"
    assert str(request.url) == "https://aitodo.co/api/trpc/video.summaryBySetting?batch=1"
    assert request.headers["Origin"] == "https://aitodo.co"
    assert request.headers["Referer"] == "https://aitodo.co/zh/"
    assert request.headers["Cookie"] == "session=abc123"
    assert prompt_config["customPrompt"] == ""
    assert "输出要求" not in prompt_config["customPrompt"]


@respx.mock
async def test_bibi_client_uses_web_summary_by_default(tmp_path) -> None:
    route = respx.post(
        "https://aitodo.co/api/trpc/video.summaryBySetting?batch=1"
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "result": {
                        "data": {
                            "json": {
                                "summary": "- Point",
                                "fromCache": True,
                            }
                        }
                    }
                }
            ],
        )
    )
    settings = Settings(
        bibigpt_base_url="https://aitodo.co/zh",
        cookie_file="",
        platform_cookie_files={"bibigpt": str(tmp_path / "missing.txt")},
    )

    result = await BibiClient(settings).summarize(
        "https://youtu.be/abc123",
        prompt="Focus on the business implications.",
    )

    request = route.calls.last.request
    body = json.loads(request.content)
    prompt_config = body["0"]["json"]["promptConfig"]

    assert result.content == "- Point"
    assert result.model == "bibigpt-web"
    assert result.from_cache is True
    assert str(request.url) == "https://aitodo.co/api/trpc/video.summaryBySetting?batch=1"
    assert request.headers["Origin"] == "https://aitodo.co"
    assert request.headers["Referer"] == "https://aitodo.co/zh/"
    assert body["0"]["json"]["url"] == "https://youtu.be/abc123"
    assert prompt_config["customPrompt"].startswith("Focus on the business implications.")
    assert "必须用简体中文输出" in prompt_config["customPrompt"]
    assert "不要使用 emoji" in prompt_config["customPrompt"]
    assert "使用 Markdown，尽量保留原有结构" in prompt_config["customPrompt"]
    assert "Markdown 各级标题都改成无序列表+加粗" in prompt_config["customPrompt"]
    assert "允许使用多级无序列表，用缩进表达层级" in prompt_config["customPrompt"]
    assert '无序列表只能使用 "-"' in prompt_config["customPrompt"]
    assert "不要使用编号列表" in prompt_config["customPrompt"]
    assert "保持简洁, 只保留最重要的信息。" not in prompt_config["customPrompt"]
    assert "避免多层 bullet, 尽量只使用单层列表。" not in prompt_config["customPrompt"]
    assert prompt_config["outputLanguage"] == "中文"
    assert prompt_config["autoTranslateLanguage"] == "中文"
    assert prompt_config["showEmoji"] is False
    assert prompt_config["detailLevel"] == 1500
    assert prompt_config["isRefresh"] is False


def test_bibi_api_error_summarizes_html_body() -> None:
    error = BibiAPIError(
        500,
        '<!DOCTYPE html><html lang="en"><head><title>Error</title></head></html>',
    )

    message = str(error)
    assert "BibiGPT API error (HTTP 500)" in message
    assert "service returned an HTML error page" in message
    assert "<!DOCTYPE html>" not in message


@respx.mock
async def test_bibi_client_rejects_missing_transcript_response(tmp_path) -> None:
    respx.post("https://aitodo.co/api/trpc/video.summaryBySetting?batch=1").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "result": {
                        "data": {
                            "json": {
                                "summary": (
                                    "Please provide the transcript you would like me "
                                    "to summarize!"
                                ),
                                "fromCache": False,
                            }
                        }
                    }
                }
            ],
        )
    )
    settings = Settings(
        bibigpt_base_url="https://aitodo.co/zh",
        cookie_file="",
        platform_cookie_files={"bibigpt": str(tmp_path / "missing.txt")},
    )

    with pytest.raises(TranscriptUnavailableError, match="did not receive a transcript"):
        await BibiClient(settings).summarize("https://youtu.be/cBBZrjwqWZc")


async def test_bibi_client_rejects_corrupt_supabase_auth_cookie(tmp_path) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "\n".join(
            [
                "# Netscape HTTP Cookie File",
                "aitodo.co\tFALSE\t/\tFALSE\t2147483647\t"
                "sb-hxtizkasyxsfnzgphrtk-auth-token.0\tbase64-abcde",
            ]
        ),
        encoding="utf-8",
    )
    settings = Settings(
        bibigpt_base_url="https://aitodo.co/zh",
        cookie_file=str(cookie_file),
    )

    with pytest.raises(AuthenticationError, match="corrupted or incomplete"):
        await BibiClient(settings).summarize("https://youtu.be/abc123")


async def test_bibi_client_browser_mode_uses_web_endpoint_with_profile(
    tmp_path,
    monkeypatch,
) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "\n".join(
            [
                "# Netscape HTTP Cookie File",
                "aitodo.co\tFALSE\t/\tFALSE\t2147483647\t"
                "sb-hxtizkasyxsfnzgphrtk-auth-token.0\tbase64-abcde",
            ]
        ),
        encoding="utf-8",
    )
    settings = Settings(
        bibigpt_base_url="https://aitodo.co/zh",
        bibigpt_access_mode="browser",
        cookie_file=str(cookie_file),
        bibigpt_browser_profile_dir=str(tmp_path / "profile"),
    )
    captured: dict[str, object] = {}

    async def fake_browser_fetch(
        self,
        url: str,
        body: dict[str, object] | None,
        *,
        method: str = "POST",
    ) -> object:
        captured["url"] = url
        captured["body"] = body
        captured["method"] = method
        return [
            {
                "result": {
                    "data": {
                        "json": {
                            "summary": "- Browser point",
                            "fromCache": False,
                        }
                    }
                }
            }
        ]

    monkeypatch.setattr(BibiClient, "_browser_fetch_json", fake_browser_fetch)

    result = await BibiClient(settings).summarize(
        "https://youtu.be/abc123",
        prompt="Use the web page.",
    )

    body = captured["body"]
    assert isinstance(body, dict)
    prompt_config = body["0"]["json"]["promptConfig"]  # type: ignore[index]

    assert result.content == "- Browser point"
    assert captured["url"] == "https://aitodo.co/api/trpc/video.summaryBySetting?batch=1"
    assert captured["method"] == "POST"
    assert body["0"]["json"]["url"] == "https://youtu.be/abc123"  # type: ignore[index]
    assert prompt_config["customPrompt"].startswith("Use the web page.")
    assert "必须用简体中文输出" in prompt_config["customPrompt"]
    assert "Markdown 各级标题都改成无序列表+加粗" in prompt_config["customPrompt"]
    assert "允许使用多级无序列表，用缩进表达层级" in prompt_config["customPrompt"]
    assert prompt_config["isRefresh"] is True


@respx.mock
async def test_bibi_client_passes_model_to_promptconfig(tmp_path) -> None:
    route = respx.post(
        "https://aitodo.co/api/trpc/video.summaryBySetting?batch=1"
    ).mock(
        return_value=httpx.Response(
            200,
            json=[{"result": {"data": {"json": {"summary": "- Point", "fromCache": False}}}}],
        )
    )
    settings = Settings(
        bibigpt_base_url="https://aitodo.co/zh",
        bibigpt_model="anthropic/claude-sonnet-4-6",
        cookie_file="",
        platform_cookie_files={"bibigpt": str(tmp_path / "missing.txt")},
    )

    await BibiClient(settings).summarize("https://youtu.be/abc123")

    body = json.loads(route.calls.last.request.content)
    assert body["0"]["json"]["promptConfig"]["model"] == "anthropic/claude-sonnet-4-6"


async def test_bibi_client_browser_passes_model_to_promptconfig(
    tmp_path,
    monkeypatch,
) -> None:
    settings = Settings(
        bibigpt_base_url="https://aitodo.co/zh",
        bibigpt_model="deepseek-v4-pro",
        bibigpt_access_mode="browser",
        cookie_file="",
        bibigpt_browser_profile_dir=str(tmp_path / "profile"),
    )
    captured: dict[str, object] = {}

    async def fake_browser_fetch(self, url, body, *, method="POST"):
        captured["body"] = body
        return [{"result": {"data": {"json": {"summary": "- Point", "fromCache": False}}}}]

    monkeypatch.setattr(BibiClient, "_browser_fetch_json", fake_browser_fetch)

    await BibiClient(settings).summarize("https://youtu.be/abc123")

    body = captured["body"]
    assert isinstance(body, dict)
    assert body["0"]["json"]["promptConfig"]["model"] == "deepseek-v4-pro"  # type: ignore[index]


async def test_bibi_client_browser_user_probe_reports_missing_login(
    tmp_path,
    monkeypatch,
) -> None:
    settings = Settings(
        bibigpt_base_url="https://aitodo.co/zh",
        bibigpt_access_mode="browser",
        cookie_file="",
        bibigpt_browser_profile_dir=str(tmp_path / "profile"),
    )

    async def fake_browser_fetch(
        self,
        url: str,
        body: dict[str, object] | None,
        *,
        method: str = "POST",
    ) -> object:
        return {"result": {"data": {"json": None}}}

    monkeypatch.setattr(BibiClient, "_browser_fetch_json", fake_browser_fetch)

    with pytest.raises(AuthenticationError, match="browser profile is not logged in"):
        await BibiClient(settings).get_user_info()
