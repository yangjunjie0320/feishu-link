import json
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import respx

import src.bibi_client as bibi_client_module
from src.bibi_client import (
    AuthenticationError,
    BibiAPIError,
    BibiClient,
    TranscriptUnavailableError,
    _resolve_routes,
    _source_url_for_log,
)
from src.bibi_models import SubtitleSegment, SummaryResult, Usage
from src.config import Settings
from src.cookie_utils import get_cookie_header


def _summary_result(
    *,
    content_id: str = "content-123",
    subtitles: tuple[SubtitleSegment, ...] = (),
) -> SummaryResult:
    return SummaryResult(
        content="- Point",
        model="bibigpt-web",
        usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
        from_cache=True,
        video_url="https://youtu.be/abc123",
        content_id=content_id,
        subtitles=subtitles,
    )


def _trpc_json(data: dict[str, Any]) -> dict[str, Any]:
    return {"result": {"data": {"json": data}}}


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
    assert _source_url_for_log(
        "https://cdn.example.test/video/1?token=secret#fragment"
    ) == "https://cdn.example.test/video/1"


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
                "subtitlesArray": [
                    {
                        "index": 7,
                        "startTime": 1.25,
                        "end": 2.5,
                        "text": " Top subtitle ",
                        "speaker_id": 2,
                    }
                ],
            },
            "top-content",
        ),
        (
            {
                "detail": {
                    "dbId": "detail-content",
                    "subtitlesArray": [
                        {
                            "index": "8",
                            "startTime": "3",
                            "endTime": "4.5",
                            "text": "Detail subtitle",
                            "speakerId": "3",
                        }
                    ],
                }
            },
            "detail-content",
        ),
        (
            {
                "videoDetail": {
                    "contentId": "video-detail-content",
                    "subtitlesArray": [
                        {
                            "index": 9,
                            "start": 5,
                            "end": 6,
                            "text": "Video detail subtitle",
                        }
                    ],
                }
            },
            "video-detail-content",
        ),
    ],
)
def test_summary_result_parses_content_id_and_embedded_subtitles(
    container: dict[str, Any],
    expected_content_id: str,
) -> None:
    result = SummaryResult.from_web_response(
        {"summary": "- Point", **container},
        video_url="https://youtu.be/abc123",
    )

    assert result.content_id == expected_content_id
    assert len(result.subtitles) == 1
    assert result.subtitles[0].text in {
        "Top subtitle",
        "Detail subtitle",
        "Video detail subtitle",
    }
    assert isinstance(result.subtitles[0].index, int)
    assert isinstance(result.subtitles[0].start_time, float)
    assert isinstance(result.subtitles[0].end_time, float)


@respx.mock
async def test_fetch_cached_subtitles_prefers_embedded_without_request(tmp_path) -> None:
    embedded = (
        SubtitleSegment(
            index=1,
            start_time=0.0,
            end_time=2.0,
            text="Cached subtitle",
        ),
    )
    settings = Settings(
        bibigpt_base_url="https://aitodo.co/zh",
        cookie_file="",
        platform_cookie_files={"bibigpt": str(tmp_path / "missing.txt")},
    )

    result = await BibiClient(settings).fetch_cached_subtitles(
        _summary_result(subtitles=embedded)
    )

    assert result.available is True
    assert result.status == "available"
    assert result.source == "embedded"
    assert result.subtitles == embedded
    assert len(respx.calls) == 0


@respx.mock
async def test_fetch_cached_subtitles_web_reads_status_with_task_id(tmp_path) -> None:
    status_route = respx.get(
        "https://aitodo.co/api/trpc/content.subtitlesTaskStatus"
    ).mock(
        return_value=httpx.Response(
            200,
            json=_trpc_json(
                {
                    "status": "completed",
                    "subtitlesArray": [
                        {
                            "index": 4,
                            "startTime": 12.5,
                            "end": 15.0,
                            "text": "Status subtitle",
                            "speaker_id": 1,
                        }
                    ],
                }
            ),
        )
    )
    info_route = respx.get("https://aitodo.co/api/trpc/content.info").mock(
        return_value=httpx.Response(200, json=_trpc_json({}))
    )
    settings = Settings(
        bibigpt_base_url="https://aitodo.co/zh",
        cookie_file="",
        platform_cookie_files={"bibigpt": str(tmp_path / "missing.txt")},
    )

    result = await BibiClient(settings).fetch_cached_subtitles(_summary_result())

    request_input = json.loads(status_route.calls.last.request.url.params["input"])
    assert request_input == {"json": {"taskId": "content-123"}}
    assert status_route.calls.last.request.method == "GET"
    assert info_route.called is False
    assert result.status == "available"
    assert result.source == "subtitles_task_status"
    assert result.subtitles == (
        SubtitleSegment(
            index=4,
            start_time=12.5,
            end_time=15.0,
            text="Status subtitle",
            speaker_id=1,
        ),
    )


@respx.mock
async def test_fetch_cached_subtitles_web_polls_then_reads_content_info(
    tmp_path,
    monkeypatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(bibi_client_module.asyncio, "sleep", fake_sleep)
    status_route = respx.get(
        "https://aitodo.co/api/trpc/content.subtitlesTaskStatus"
    ).mock(
        return_value=httpx.Response(
            200,
            json=_trpc_json({"status": "processing"}),
        )
    )
    info_route = respx.get("https://aitodo.co/api/trpc/content.info").mock(
        return_value=httpx.Response(
            200,
            json=_trpc_json(
                {
                    "videoDetail": {
                        "subtitlesArray": [
                            {
                                "index": 6,
                                "startTime": 20,
                                "end": 21,
                                "text": "Info subtitle",
                            }
                        ]
                    }
                }
            ),
        )
    )
    settings = Settings(
        bibigpt_base_url="https://aitodo.co/zh",
        cookie_file="",
        platform_cookie_files={"bibigpt": str(tmp_path / "missing.txt")},
    )

    result = await BibiClient(settings).fetch_cached_subtitles(_summary_result())

    info_input = json.loads(info_route.calls.last.request.url.params["input"])
    assert status_route.call_count == 3
    assert sleeps == [2, 2]
    assert info_route.calls.last.request.method == "GET"
    assert info_input == {
        "json": {
            "url": "https://youtu.be/abc123",
            "contentId": "content-123",
            "skipSubtitleTask": True,
            "isRefresh": False,
        }
    }
    assert result.status == "available"
    assert result.source == "content_info"
    assert result.subtitles[0].text == "Info subtitle"


@respx.mock
async def test_fetch_cached_subtitles_web_polls_null_status_before_content_info(
    tmp_path,
    monkeypatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(bibi_client_module.asyncio, "sleep", fake_sleep)
    status_route = respx.get(
        "https://aitodo.co/api/trpc/content.subtitlesTaskStatus"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"result": {"data": {"json": None}}},
        )
    )
    info_route = respx.get("https://aitodo.co/api/trpc/content.info").mock(
        return_value=httpx.Response(200, json=_trpc_json({"detail": {}}))
    )
    settings = Settings(
        bibigpt_base_url="https://aitodo.co/zh",
        cookie_file="",
        platform_cookie_files={"bibigpt": str(tmp_path / "missing.txt")},
    )

    result = await BibiClient(settings).fetch_cached_subtitles(_summary_result())

    assert status_route.call_count == 3
    assert sleeps == [2, 2]
    assert info_route.call_count == 1
    assert result.status == "unavailable"
    assert result.subtitles == ()


@respx.mock
async def test_fetch_cached_subtitles_returns_unavailable_when_cache_is_empty(tmp_path) -> None:
    status_route = respx.get(
        "https://aitodo.co/api/trpc/content.subtitlesTaskStatus"
    ).mock(
        return_value=httpx.Response(
            200,
            json=_trpc_json({"status": "completed"}),
        )
    )
    info_route = respx.get("https://aitodo.co/api/trpc/content.info").mock(
        return_value=httpx.Response(200, json=_trpc_json({"detail": {}}))
    )
    settings = Settings(
        bibigpt_base_url="https://aitodo.co/zh",
        cookie_file="",
        platform_cookie_files={"bibigpt": str(tmp_path / "missing.txt")},
    )

    result = await BibiClient(settings).fetch_cached_subtitles(_summary_result())

    assert status_route.call_count == 1
    assert info_route.call_count == 1
    assert result.available is False
    assert result.status == "unavailable"
    assert result.source == "none"
    assert "no cached subtitles" in result.reason


@respx.mock
async def test_fetch_cached_subtitles_returns_error_instead_of_raising(tmp_path) -> None:
    respx.get("https://aitodo.co/api/trpc/content.subtitlesTaskStatus").mock(
        return_value=httpx.Response(503, text="signed-url-should-not-leak")
    )
    respx.get("https://aitodo.co/api/trpc/content.info").mock(
        return_value=httpx.Response(500, text="another-sensitive-body")
    )
    settings = Settings(
        bibigpt_base_url="https://aitodo.co/zh",
        cookie_file="",
        platform_cookie_files={"bibigpt": str(tmp_path / "missing.txt")},
    )

    result = await BibiClient(settings).fetch_cached_subtitles(_summary_result())

    assert result.status == "error"
    assert result.subtitles == ()
    assert "signed-url-should-not-leak" not in result.reason
    assert "another-sensitive-body" not in result.reason


async def test_fetch_cached_subtitles_missing_content_id_skips_lookup(tmp_path) -> None:
    settings = Settings(
        bibigpt_base_url="https://aitodo.co/zh",
        cookie_file="",
        platform_cookie_files={"bibigpt": str(tmp_path / "missing.txt")},
    )

    result = await BibiClient(settings).fetch_cached_subtitles(
        _summary_result(content_id="")
    )

    assert result.status == "unavailable"
    assert result.source == "none"
    assert "contentId" in result.reason


async def test_fetch_cached_subtitles_browser_reuses_one_context_for_fallback(
    tmp_path,
    monkeypatch,
) -> None:
    browser_context_entries = 0
    requests: list[dict[str, Any]] = []

    class FakePage:
        async def evaluate(self, script: str, request: dict[str, Any]) -> dict[str, Any]:
            del script
            requests.append(request)
            url = httpx.URL(str(request["url"]))
            if url.path.endswith("content.subtitlesTaskStatus"):
                data = {"status": "completed"}
            else:
                data = {
                    "detail": {
                        "subtitlesArray": [
                            {
                                "index": 10,
                                "startTime": 30,
                                "end": 32,
                                "text": "Browser subtitle",
                            }
                        ]
                    }
                }
            return {
                "status": 200,
                "ok": True,
                "text": json.dumps(_trpc_json(data)),
            }

    @asynccontextmanager
    async def fake_browser_page(self: BibiClient):
        nonlocal browser_context_entries
        del self
        browser_context_entries += 1
        yield FakePage()

    monkeypatch.setattr(BibiClient, "_browser_page", fake_browser_page)
    settings = Settings(
        bibigpt_base_url="https://aitodo.co/zh",
        bibigpt_access_mode="browser",
        cookie_file="",
        bibigpt_browser_profile_dir=str(tmp_path / "profile"),
    )

    result = await BibiClient(settings).fetch_cached_subtitles(_summary_result())

    status_input = json.loads(httpx.URL(str(requests[0]["url"])).params["input"])
    info_input = json.loads(httpx.URL(str(requests[1]["url"])).params["input"])
    assert browser_context_entries == 1
    assert len(requests) == 2
    assert all(request["method"] == "GET" for request in requests)
    assert all(request["body"] is None for request in requests)
    assert status_input == {"json": {"taskId": "content-123"}}
    assert info_input["json"]["skipSubtitleTask"] is True
    assert info_input["json"]["isRefresh"] is False
    assert result.status == "available"
    assert result.source == "content_info"
    assert result.subtitles[0].text == "Browser subtitle"
