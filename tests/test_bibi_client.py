import json
import time
from typing import Any

import httpx
import pytest
import respx

from src.bibi_client import (
    AuthenticationError,
    BibiAPIError,
    BibiClient,
    BibiTimeoutError,
    TranscriptUnavailableError,
    _resolve_routes,
    _source_url_for_log,
)
from src.bibi_models import ChapterSummarySection, SummaryResult, Usage
from src.config import Settings
from src.cookie_utils import get_cookie_header


def _summary_result(
    *,
    content_id: str = "content-123",
) -> SummaryResult:
    return SummaryResult(
        content="- Point",
        model="bibigpt-web",
        usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
        from_cache=True,
        video_url="https://youtu.be/abc123",
        content_id=content_id,
    )


def _trpc_json(data: dict[str, Any]) -> dict[str, Any]:
    return {"result": {"data": {"json": data}}}


@respx.mock
async def test_bibi_client_routes_locale_base_url_to_origin_web(tmp_path) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "\n".join(
            [
                "# Netscape HTTP Cookie File",
                ".aitodo.co\tTRUE\t/\tFALSE\t2147483647\tsession\tabc123",
                ".bibigpt.co\tTRUE\t/\tFALSE\t2147483647\tsession\twrong",
            ]
        ),
        encoding="utf-8",
    )
    route = respx.post("https://aitodo.co/api/trpc/video.summaryBySetting?batch=1").mock(
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
    route = respx.post("https://aitodo.co/api/trpc/video.summaryBySetting?batch=1").mock(
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


def test_resolve_routes_locale_path_appends_desktop_for_browser() -> None:
    routes = _resolve_routes("https://aitodo.co/zh")

    assert routes.api_base_url == "https://aitodo.co"
    assert routes.referer == "https://aitodo.co/zh/"
    assert routes.browser_page_url == "https://aitodo.co/zh/desktop"
    assert routes.origin == "https://aitodo.co"
    assert routes.cookie_domain == "aitodo.co"


def test_resolve_routes_root_base_url() -> None:
    routes = _resolve_routes("https://bibigpt.co")

    assert routes.api_base_url == "https://bibigpt.co"
    assert routes.referer == "https://bibigpt.co/"
    assert routes.browser_page_url == "https://bibigpt.co"


def test_source_url_for_log_removes_query_and_fragment() -> None:
    assert (
        _source_url_for_log("https://cdn.example.test/video/1?token=secret#fragment")
        == "https://cdn.example.test/video/1"
    )


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
                                    "Please provide the transcript you would like me to summarize!"
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
                            "contentId": "content-123",
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
    route = respx.post("https://aitodo.co/api/trpc/video.summaryBySetting?batch=1").mock(
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
        return [
            {
                "result": {
                    "data": {
                        "json": {
                            "summary": "- Point",
                            "fromCache": False,
                            "contentId": "content-123",
                        }
                    }
                }
            }
        ]

    monkeypatch.setattr(BibiClient, "_browser_fetch_json", fake_browser_fetch)

    await BibiClient(settings).summarize("https://youtu.be/abc123")

    body = captured["body"]
    assert isinstance(body, dict)
    assert body["0"]["json"]["promptConfig"]["model"] == "deepseek-v4-pro"  # type: ignore[index]


async def test_bibi_client_browser_recovers_missing_content_id(
    tmp_path,
    monkeypatch,
) -> None:
    settings = Settings(
        bibigpt_base_url="https://aitodo.co/zh",
        bibigpt_access_mode="browser",
        cookie_file="",
        bibigpt_browser_profile_dir=str(tmp_path / "profile"),
    )
    calls: list[dict[str, Any]] = []

    async def fake_browser_fetch(self, url, body, *, method="POST"):
        calls.append(body)
        payload: dict[str, Any] = {"summary": "- Point", "fromCache": False}
        if len(calls) > 1:
            payload["contentId"] = "recovered-456"
        return [{"result": {"data": {"json": payload}}}]

    monkeypatch.setattr(BibiClient, "_browser_fetch_json", fake_browser_fetch)
    monkeypatch.setattr("src.bibi_client._CONTENT_ID_RECOVERY_DELAYS", (0.0,))

    result = await BibiClient(settings).summarize("https://youtu.be/abc123")

    assert result.content_id == "recovered-456"
    assert result.content == "- Point"
    assert len(calls) == 2
    assert calls[0]["0"]["json"]["promptConfig"]["isRefresh"] is True
    assert calls[1]["0"]["json"]["promptConfig"]["isRefresh"] is False


async def test_bibi_client_browser_content_id_recovery_exhausts_retries(
    tmp_path,
    monkeypatch,
) -> None:
    settings = Settings(
        bibigpt_base_url="https://aitodo.co/zh",
        bibigpt_access_mode="browser",
        cookie_file="",
        bibigpt_browser_profile_dir=str(tmp_path / "profile"),
    )
    calls: list[dict[str, Any]] = []

    async def fake_browser_fetch(self, url, body, *, method="POST"):
        calls.append(body)
        return [{"result": {"data": {"json": {"summary": "- Point", "fromCache": False}}}}]

    monkeypatch.setattr(BibiClient, "_browser_fetch_json", fake_browser_fetch)
    monkeypatch.setattr("src.bibi_client._CONTENT_ID_RECOVERY_DELAYS", (0.0, 0.0))

    result = await BibiClient(settings).summarize("https://youtu.be/abc123")

    assert result.content_id == ""
    assert result.content == "- Point"
    assert len(calls) == 3


def _browser_settings(tmp_path) -> Settings:
    return Settings(
        bibigpt_base_url="https://aitodo.co/zh",
        bibigpt_access_mode="browser",
        cookie_file="",
        bibigpt_browser_profile_dir=str(tmp_path / "profile"),
    )


def _scripted_browser_fetch(monkeypatch, script: list[Any]) -> list[dict[str, Any]]:
    """Each script entry is either an exception to raise or a summary string
    to return (with a contentId so contentId recovery never kicks in)."""
    calls: list[dict[str, Any]] = []

    async def fake_browser_fetch(self, url, body, *, method="POST"):
        calls.append(body)
        step = script[len(calls) - 1]
        if isinstance(step, BaseException):
            raise step
        payload = {"summary": step, "fromCache": False, "contentId": "cid-1"}
        return [{"result": {"data": {"json": payload}}}]

    monkeypatch.setattr(BibiClient, "_browser_fetch_json", fake_browser_fetch)
    monkeypatch.setattr("src.bibi_client._RECOVERY_DELAYS", (0.0, 0.0, 0.0))
    return calls


def _is_refresh(call: dict[str, Any]) -> bool:
    return call["0"]["json"]["promptConfig"]["isRefresh"]


async def test_summarize_forces_regeneration_on_first_request(tmp_path, monkeypatch) -> None:
    calls = _scripted_browser_fetch(monkeypatch, ["- ok"])

    result = await BibiClient(_browser_settings(tmp_path)).summarize("https://youtu.be/abc")

    assert result.content == "- ok"
    assert [_is_refresh(c) for c in calls] == [True]


async def test_summarize_custom_prompt_forces_regeneration(tmp_path, monkeypatch) -> None:
    calls = _scripted_browser_fetch(monkeypatch, ["- ok"])

    await BibiClient(_browser_settings(tmp_path)).summarize("https://youtu.be/abc", prompt="要点")

    assert [_is_refresh(c) for c in calls] == [True]
    assert "要点" in calls[0]["0"]["json"]["promptConfig"]["customPrompt"]


async def test_summarize_recovers_via_lookup_after_transient_500(tmp_path, monkeypatch) -> None:
    failed = BibiAPIError(500, '{"message":"Connection error."}')
    calls = _scripted_browser_fetch(monkeypatch, [failed, failed, "- late"])

    result = await BibiClient(_browser_settings(tmp_path)).summarize("https://youtu.be/abc")

    assert result.content == "- late"
    assert [_is_refresh(c) for c in calls] == [True, False, False]


async def test_summarize_custom_prompt_recovery_drops_refresh(tmp_path, monkeypatch) -> None:
    calls = _scripted_browser_fetch(monkeypatch, [BibiAPIError(500, "blocked"), "- late"])

    result = await BibiClient(_browser_settings(tmp_path)).summarize(
        "https://youtu.be/abc", prompt="要点"
    )

    assert result.content == "- late"
    assert [_is_refresh(c) for c in calls] == [True, False]


async def test_summarize_preserves_last_error_when_recovery_exhausted(
    tmp_path, monkeypatch
) -> None:
    blocked = BibiAPIError(500, "blocked")
    last_error = BibiAPIError(524, "cf")
    calls = _scripted_browser_fetch(
        monkeypatch, [blocked, BibiAPIError(502, "gw"), blocked, last_error]
    )

    with pytest.raises(BibiAPIError) as excinfo:
        await BibiClient(_browser_settings(tmp_path)).summarize("https://youtu.be/abc")

    assert excinfo.value is last_error
    assert len(calls) == 4


@pytest.mark.parametrize(
    "error",
    [AuthenticationError(401, "expired"), TranscriptUnavailableError(200, "no transcript")],
)
async def test_summarize_does_not_retry_auth_or_transcript_errors(
    tmp_path, monkeypatch, error
) -> None:
    calls = _scripted_browser_fetch(monkeypatch, [error, "- never"])

    with pytest.raises(type(error)):
        await BibiClient(_browser_settings(tmp_path)).summarize("https://youtu.be/abc")

    assert len(calls) == 1


async def test_summarize_does_not_retry_unrecoverable_status(tmp_path, monkeypatch) -> None:
    calls = _scripted_browser_fetch(monkeypatch, [BibiAPIError(402, "quota"), "- never"])

    with pytest.raises(BibiAPIError) as excinfo:
        await BibiClient(_browser_settings(tmp_path)).summarize("https://youtu.be/abc")

    assert excinfo.value.status_code == 402
    assert len(calls) == 1


@pytest.mark.parametrize(
    "error", [BibiAPIError(524, "cloudflare"), BibiTimeoutError(0, "browser timed out")]
)
async def test_summarize_recovers_stored_result_after_timeout(tmp_path, monkeypatch, error) -> None:
    calls = _scripted_browser_fetch(monkeypatch, [error, "", "- late"])

    result = await BibiClient(_browser_settings(tmp_path)).summarize("https://youtu.be/abc")

    assert result.content == "- late"
    assert [_is_refresh(c) for c in calls] == [True, False, False]


async def test_summarize_treats_empty_first_response_as_pending(tmp_path, monkeypatch) -> None:
    calls = _scripted_browser_fetch(monkeypatch, ["", "- late"])

    result = await BibiClient(_browser_settings(tmp_path)).summarize("https://youtu.be/abc")

    assert result.content == "- late"
    assert len(calls) == 2


async def test_web_mode_timeout_raises_bibi_timeout_error(tmp_path) -> None:
    settings = Settings(
        bibigpt_base_url="https://aitodo.co/zh",
        bibigpt_access_mode="web",
        cookie_file="",
        bibigpt_timeout=0.001,
    )
    client = BibiClient(settings)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r".*summaryBySetting.*").mock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(BibiTimeoutError):
            await client._summarize_web("https://youtu.be/abc", "")


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


async def test_bibi_client_writes_back_cookies(tmp_path) -> None:
    target = tmp_path / "bibigpt.txt"
    client = BibiClient(
        Settings(
            bibigpt_base_url="https://aitodo.co/zh",
            cookie_file="",
            platform_cookie_files={"bibigpt": str(target)},
        )
    )

    class FakeContext:
        async def cookies(self):
            return [
                {
                    "name": "sb-aitodo-auth-token.0",
                    "value": "fresh",
                    "domain": "aitodo.co",
                    "path": "/",
                    "expires": time.time() + 86400,
                    "secure": True,
                    "httpOnly": True,
                },
                {"name": "noise", "value": "n", "domain": ".other.com", "path": "/", "expires": -1},
            ]

    await client._writeback_cookies(FakeContext())

    header = get_cookie_header(str(target), "aitodo.co")
    assert "sb-aitodo-auth-token.0=fresh" in header
    assert "noise" not in header


@pytest.mark.parametrize(
    ("container", "expected_content_id"),
    [
        (
            {
                "contentId": "top-content",
                "subtitlesArray": [{"text": "must not be retained"}],
            },
            "top-content",
        ),
        (
            {
                "detail": {
                    "dbId": "detail-content",
                    "subtitlesArray": [{"text": "must not be retained"}],
                }
            },
            "detail-content",
        ),
        (
            {
                "videoDetail": {
                    "contentId": "video-detail-content",
                    "subtitlesArray": [{"text": "must not be retained"}],
                }
            },
            "video-detail-content",
        ),
    ],
)
def test_summary_result_parses_content_id_without_retaining_embedded_subtitles(
    container: dict[str, Any],
    expected_content_id: str,
) -> None:
    result = SummaryResult.from_web_response(
        {"summary": "- Point", **container},
        video_url="https://youtu.be/abc123",
    )

    assert result.content_id == expected_content_id
    assert not hasattr(result, "subtitles")


@respx.mock
async def test_fetch_chapter_summary_web_uses_exact_timeline_query(tmp_path) -> None:
    route = respx.get("https://aitodo.co/api/trpc/video.chapterSummary").mock(
        return_value=httpx.Response(
            200,
            json=_trpc_json(
                {
                    "chapterSummary": " 视频总述 ",
                    "chapters": [
                        {
                            "start": 0,
                            "end": 266.8,
                            "title": " 项目背景 ",
                            "summary": " 介绍换挡器选择。 ",
                            "text": "raw text must be ignored",
                            "contents": [[0, 1, "raw subtitle must be ignored"]],
                        },
                        {
                            "start": "266.8",
                            "end": "602.32",
                            "title": "台架测试",
                            "summary": "说明测试过程。",
                        },
                    ],
                    "subtitlesArray": [{"text": "raw subtitle must be ignored"}],
                }
            ),
        )
    )
    settings = Settings(
        bibigpt_base_url="https://aitodo.co/zh",
        cookie_file="",
        platform_cookie_files={"bibigpt": str(tmp_path / "missing.txt")},
    )

    result = await BibiClient(settings).fetch_chapter_summary(_summary_result())

    request = route.calls.last.request
    request_input = json.loads(request.url.params["input"])
    assert request.method == "GET"
    assert request_input == {
        "json": {
            "contentId": "content-123",
            "outputLanguage": "中文",
            "summaryType": "timeline",
        }
    }
    assert "retryFlag" not in request.url.params["input"]
    assert result.available is True
    assert result.status == "available"
    assert result.source == "video.chapterSummary"
    assert result.introduction == "视频总述"
    assert result.sections == (
        ChapterSummarySection(
            index=0,
            start_time=0.0,
            end_time=266.8,
            title="项目背景",
            summary="介绍换挡器选择。",
        ),
        ChapterSummarySection(
            index=1,
            start_time=266.8,
            end_time=602.32,
            title="台架测试",
            summary="说明测试过程。",
        ),
    )


@respx.mock
async def test_fetch_chapter_summary_skips_invalid_top_level_sections(tmp_path) -> None:
    respx.get("https://aitodo.co/api/trpc/video.chapterSummary").mock(
        return_value=httpx.Response(
            200,
            json=_trpc_json(
                {
                    "chapterSummary": "总述",
                    "chapters": [
                        {
                            "start": 0,
                            "end": 1,
                            "title": "",
                            "summary": "缺标题",
                        },
                        {
                            "start": -1,
                            "end": 1,
                            "title": "负时间",
                            "summary": "无效",
                        },
                        {
                            "start": 8,
                            "end": 4,
                            "title": "倒序",
                            "summary": "无效",
                        },
                        {
                            "start": 12.5,
                            "end": 15,
                            "title": "有效章节",
                            "summary": "有效摘要",
                        },
                    ],
                }
            ),
        )
    )
    settings = Settings(
        bibigpt_base_url="https://aitodo.co/zh",
        cookie_file="",
        platform_cookie_files={"bibigpt": str(tmp_path / "missing.txt")},
    )

    result = await BibiClient(settings).fetch_chapter_summary(_summary_result())

    assert result.sections == (
        ChapterSummarySection(
            index=3,
            start_time=12.5,
            end_time=15.0,
            title="有效章节",
            summary="有效摘要",
        ),
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"chapterSummary": "仅有总述", "chapters": []},
        {
            "detail": {
                "chapterSummary": "嵌套总述不能读取",
                "chapters": [
                    {
                        "start": 0,
                        "end": 1,
                        "title": "嵌套章节",
                        "summary": "不能读取",
                    }
                ],
            }
        },
        {
            "chapterSummary": "总述",
            "chapters": [
                {
                    "start": 0,
                    "end": 1,
                    "title": "只有原文",
                    "text": "不能代替 summary",
                    "contents": [[0, 1, "不能读取"]],
                }
            ],
            "subtitlesArray": [{"text": "不能读取"}],
        },
    ],
)
@respx.mock
async def test_fetch_chapter_summary_returns_unavailable_without_valid_top_level_section(
    tmp_path,
    payload: dict[str, Any],
) -> None:
    route = respx.get("https://aitodo.co/api/trpc/video.chapterSummary").mock(
        return_value=httpx.Response(200, json=_trpc_json(payload))
    )
    settings = Settings(
        bibigpt_base_url="https://aitodo.co/zh",
        cookie_file="",
        platform_cookie_files={"bibigpt": str(tmp_path / "missing.txt")},
    )

    result = await BibiClient(settings).fetch_chapter_summary(_summary_result())

    assert route.call_count == 1
    assert result.available is False
    assert result.status == "unavailable"
    assert result.source == "none"
    assert result.sections == ()
    assert "no timeline chapter summary" in result.reason


@respx.mock
async def test_fetch_chapter_summary_returns_unavailable_for_null_payload(tmp_path) -> None:
    route = respx.get("https://aitodo.co/api/trpc/video.chapterSummary").mock(
        return_value=httpx.Response(
            200,
            json={"result": {"data": {"json": None}}},
        )
    )
    settings = Settings(
        bibigpt_base_url="https://aitodo.co/zh",
        cookie_file="",
        platform_cookie_files={"bibigpt": str(tmp_path / "missing.txt")},
    )

    result = await BibiClient(settings).fetch_chapter_summary(_summary_result())

    assert route.call_count == 1
    assert result.status == "unavailable"
    assert result.sections == ()


@respx.mock
async def test_fetch_chapter_summary_returns_error_instead_of_raising(tmp_path) -> None:
    route = respx.get("https://aitodo.co/api/trpc/video.chapterSummary").mock(
        return_value=httpx.Response(503, text="signed-url-should-not-leak")
    )
    settings = Settings(
        bibigpt_base_url="https://aitodo.co/zh",
        cookie_file="",
        platform_cookie_files={"bibigpt": str(tmp_path / "missing.txt")},
    )

    result = await BibiClient(settings).fetch_chapter_summary(_summary_result())

    assert route.call_count == 1
    assert result.available is False
    assert result.status == "error"
    assert result.source == "none"
    assert result.sections == ()
    assert "server error" in result.reason
    assert "signed-url-should-not-leak" not in result.reason


@respx.mock
async def test_fetch_chapter_summary_explains_trpc_quota_error_without_leaking(
    tmp_path,
    caplog,
) -> None:
    route = respx.get("https://aitodo.co/api/trpc/video.chapterSummary").mock(
        return_value=httpx.Response(
            200,
            json={
                "error": {
                    "json": {
                        "message": "Payment Required: signed-url-should-not-leak",
                        "data": {
                            "code": "PAYMENT_REQUIRED",
                            "httpStatus": 402,
                            "signedUrl": "https://private.example/token",
                        },
                    }
                }
            },
        )
    )
    settings = Settings(
        bibigpt_base_url="https://aitodo.co/zh",
        cookie_file="",
        platform_cookie_files={"bibigpt": str(tmp_path / "missing.txt")},
    )

    with caplog.at_level("WARNING"):
        result = await BibiClient(settings).fetch_chapter_summary(_summary_result())

    assert route.call_count == 1
    assert result.status == "error"
    assert result.reason == (
        "BibiGPT chapter summary quota is exhausted or paid access is required."
    )
    assert "signed-url-should-not-leak" not in caplog.text
    assert "private.example" not in caplog.text


async def test_fetch_chapter_summary_authentication_error_is_returned(tmp_path) -> None:
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

    result = await BibiClient(settings).fetch_chapter_summary(_summary_result())

    assert result.status == "error"
    assert result.sections == ()
    assert result.reason == "BibiGPT authentication failed."


@respx.mock
async def test_fetch_chapter_summary_missing_content_id_skips_lookup(tmp_path) -> None:
    settings = Settings(
        bibigpt_base_url="https://aitodo.co/zh",
        cookie_file="",
        platform_cookie_files={"bibigpt": str(tmp_path / "missing.txt")},
    )

    result = await BibiClient(settings).fetch_chapter_summary(_summary_result(content_id=""))

    assert result.status == "unavailable"
    assert result.source == "none"
    assert result.sections == ()
    assert "contentId" in result.reason
    assert len(respx.calls) == 0


async def test_fetch_chapter_summary_browser_uses_profile_request(
    tmp_path,
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_browser_fetch(
        self: BibiClient,
        url: str,
        body: dict[str, Any] | None,
        *,
        method: str = "POST",
    ) -> Any:
        del self
        captured.update(url=url, body=body, method=method)
        return _trpc_json(
            {
                "chapterSummary": "浏览器总述",
                "chapters": [
                    {
                        "start": 30,
                        "end": 32,
                        "title": "浏览器章节",
                        "summary": "浏览器摘要",
                    }
                ],
            }
        )

    monkeypatch.setattr(BibiClient, "_browser_fetch_json", fake_browser_fetch)
    settings = Settings(
        bibigpt_base_url="https://aitodo.co/zh",
        bibigpt_access_mode="browser",
        cookie_file="",
        bibigpt_browser_profile_dir=str(tmp_path / "profile"),
    )

    result = await BibiClient(settings).fetch_chapter_summary(_summary_result())

    url = httpx.URL(captured["url"])
    request_input = json.loads(url.params["input"])
    assert url.path == "/api/trpc/video.chapterSummary"
    assert captured["method"] == "GET"
    assert captured["body"] is None
    assert request_input == {
        "json": {
            "contentId": "content-123",
            "outputLanguage": "中文",
            "summaryType": "timeline",
        }
    }
    assert result.status == "available"
    assert result.introduction == "浏览器总述"
    assert result.sections == (
        ChapterSummarySection(
            index=0,
            start_time=30.0,
            end_time=32.0,
            title="浏览器章节",
            summary="浏览器摘要",
        ),
    )


def test_share_page_url_prefers_content_page_when_content_id_known() -> None:
    from src.bibi_client import share_page_url

    assert (
        share_page_url("https://aitodo.co/zh", "https://youtu.be/abc", "9770f307-9430")
        == "https://aitodo.co/content/9770f307-9430"
    )


def test_share_page_url_falls_back_to_prefix_form_without_content_id() -> None:
    from src.bibi_client import share_page_url

    assert (
        share_page_url("https://aitodo.co/zh", "https://youtu.be/abc")
        == "https://aitodo.co/zh/https://youtu.be/abc"
    )
    assert (
        share_page_url("https://bibigpt.co", "https://youtu.be/abc")
        == "https://bibigpt.co/https://youtu.be/abc"
    )


def _browser_settings(tmp_path) -> Settings:
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
    return Settings(
        bibigpt_base_url="https://aitodo.co/zh",
        bibigpt_access_mode="browser",
        cookie_file=str(cookie_file),
        bibigpt_browser_profile_dir=str(tmp_path / "profile"),
    )


async def test_summarize_cached_browser_sends_is_refresh_false(monkeypatch, tmp_path) -> None:
    settings = _browser_settings(tmp_path)
    captured: dict[str, object] = {}

    async def fake_browser_fetch(self, url, body, *, method="POST"):
        captured["body"] = body
        return [
            {
                "result": {
                    "data": {
                        "json": {
                            "summary": "- Cached point",
                            "fromCache": True,
                            "contentId": "content-123",
                        }
                    }
                }
            }
        ]

    monkeypatch.setattr(BibiClient, "_browser_fetch_json", fake_browser_fetch)

    result = await BibiClient(settings).summarize_cached("https://youtu.be/abc123")

    assert result is not None
    assert result.content == "- Cached point"
    assert result.from_cache is True
    assert result.content_id == "content-123"
    prompt_config = captured["body"]["0"]["json"]["promptConfig"]  # type: ignore[index]
    assert prompt_config["isRefresh"] is False


async def test_summarize_cached_returns_none_on_lookup_failure(monkeypatch, tmp_path) -> None:
    settings = _browser_settings(tmp_path)

    async def fake_browser_fetch(self, url, body, *, method="POST"):
        raise RuntimeError("browser exploded")

    monkeypatch.setattr(BibiClient, "_browser_fetch_json", fake_browser_fetch)

    assert await BibiClient(settings).summarize_cached("https://youtu.be/abc123") is None


async def test_summarize_cached_returns_none_on_empty_summary(monkeypatch, tmp_path) -> None:
    settings = _browser_settings(tmp_path)

    async def fake_browser_fetch(self, url, body, *, method="POST"):
        return [{"result": {"data": {"json": {"summary": ""}}}}]

    monkeypatch.setattr(BibiClient, "_browser_fetch_json", fake_browser_fetch)

    assert await BibiClient(settings).summarize_cached("https://youtu.be/abc123") is None
