import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest

from src.bibi_client import (
    AuthenticationError,
    BibiAPIError,
    BibiClient,
    BibiContentPendingError,
    BibiContentTaskError,
    BibiTimeoutError,
)
from src.bibi_models import SummaryResult
from src.config import Settings

_VIDEO_URL = "https://www.bilibili.com/video/BV1test123456"
_CONTENT_ID = "server-content-id"


def _client(tmp_path: Path, **overrides: Any) -> BibiClient:
    settings = Settings(
        bibigpt_access_mode="browser",
        bibigpt_base_url="https://aitodo.co/zh",
        cookie_file=str(tmp_path / "missing-cookies.txt"),
        platform_cookie_files={},
        bibigpt_model="configured-model",
    ).model_copy(
        update={
            "bibigpt_web_queue_poll_seconds": 0.01,
            "bibigpt_web_queue_wait_seconds": 2,
            **overrides,
        }
    )
    return BibiClient(settings)


def _fetched() -> dict[str, Any]:
    return {"detail": {"dbId": _CONTENT_ID}}


def _status(status: str, **extra: Any) -> dict[str, Any]:
    return {"subtitle": {"status": status, **extra}}


def _result() -> SummaryResult:
    return SummaryResult.from_web_response(
        {"summary": "- completed", "contentId": _CONTENT_ID}, video_url=_VIDEO_URL
    )


@pytest.mark.parametrize("operation,query", [("fetch", False), ("observe", True)])
async def test_pipeline_uses_current_trpc_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, query: bool
) -> None:
    client = _client(tmp_path)
    payload = {"contentId": "id with space & 中文"}
    wire = AsyncMock(return_value=[{"result": {"data": {"json": {"accepted": True}}}}])
    monkeypatch.setattr(client, "_browser_fetch_json", wire)

    result = await client._content_pipeline_call(operation, payload, query=query)

    assert result == {"accepted": True}

    args, kwargs = wire.await_args
    url = urlsplit(args[0])
    assert url.scheme == "https"
    assert url.netloc == "aitodo.co"
    assert url.path == f"/api/trpc/contentPipeline.{operation}"
    if query:
        assert args[1] is None
        assert kwargs == {"method": "GET"}
        assert json.loads(parse_qs(url.query)["input"][0]) == {"json": payload}
    else:
        assert parse_qs(url.query) == {"batch": ["1"]}
        assert args[1] == {"0": {"json": payload}}
        assert kwargs.get("method", "POST") == "POST"


async def test_bilibili_waits_for_server_ready_before_one_configured_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path)
    calls: list[tuple[str, dict[str, Any], bool]] = []
    responses = iter(
        [
            _fetched(),
            _status("pending"),
            _status("running"),
            _status("ready"),
            {"summaryText": "  - completed  ", "id": "a-different-response-id"},
        ]
    )

    async def request(
        operation: str, payload: dict[str, Any], *, query: bool = False
    ) -> dict[str, Any]:
        calls.append((operation, payload.copy(), query))
        return next(responses)

    monkeypatch.setattr(client, "_content_pipeline_call", request)
    direct = AsyncMock(side_effect=AssertionError("must not start synchronous summary"))
    monkeypatch.setattr(client, "_summarize_once", direct)

    result = await client.summarize(_VIDEO_URL, prompt="Preserve the technical examples")

    assert [call[0] for call in calls] == ["fetch", "observe", "observe", "observe", "summarize"]
    assert calls[0][1] == {
        "url": _VIDEO_URL,
        "target": "subtitle",
        "forceFresh": False,
        "audioConfig": {"audioLanguage": "auto", "transcribeProvider": "auto"},
        "includeDetail": True,
    }
    assert all(call[1:] == ({"contentId": _CONTENT_ID}, True) for call in calls[1:4])
    summary_payload = calls[-1][1]
    assert set(summary_payload) == {"url", "promptConfig"}
    assert summary_payload["url"] == _VIDEO_URL
    assert summary_payload["promptConfig"]["model"] == "configured-model"
    assert summary_payload["promptConfig"]["customPrompt"].startswith(
        "Preserve the technical examples"
    )
    assert summary_payload["promptConfig"]["isRefresh"] is True
    assert result.content == "- completed"
    assert result.content_id == _CONTENT_ID
    direct.assert_not_awaited()


@pytest.mark.parametrize(
    "temporary_error",
    [BibiAPIError(503, "upstream unavailable"), BibiTimeoutError(0, "observe timed out")],
)
async def test_observation_temporary_error_preserves_accepted_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, temporary_error: BibiAPIError
) -> None:
    client = _client(tmp_path)
    request = AsyncMock(
        side_effect=[_fetched(), temporary_error, _status("running"), _status("ready")]
    )
    monkeypatch.setattr(client, "_content_pipeline_call", request)

    prepared = await client._prepare_content(_VIDEO_URL)
    assert prepared.content_id == _CONTENT_ID
    assert prepared.summary_via_pipeline is True

    assert [call.args[0] for call in request.await_args_list] == [
        "fetch", "observe", "observe", "observe"
    ]
    assert all(
        call.args[1] == {"contentId": _CONTENT_ID}
        for call in request.await_args_list[1:]
    )


@pytest.mark.parametrize("error_class", ["download_failed", "auth_required"])
async def test_terminal_subtitle_failure_stops_without_summary_or_new_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_class: str
) -> None:
    client = _client(tmp_path)
    request = AsyncMock(
        side_effect=[
            _fetched(),
            _status("failed", errorClass=error_class, errorMessage="specific server reason"),
        ]
    )
    monkeypatch.setattr(client, "_content_pipeline_call", request)
    with pytest.raises(BibiContentTaskError, match="specific server reason") as error:
        await client.summarize(_VIDEO_URL)

    assert error.value.error_class == error_class
    assert error.value.content_id == _CONTENT_ID
    assert not isinstance(error.value, AuthenticationError)
    assert [call.args[0] for call in request.await_args_list] == ["fetch", "observe"]


async def test_watchdog_requeues_at_most_twice_using_the_accepted_content_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path)
    fetch_payloads: list[dict[str, Any]] = []

    async def request(
        operation: str, payload: dict[str, Any], *, query: bool = False
    ) -> dict[str, Any]:
        if operation == "fetch":
            fetch_payloads.append(payload.copy())
            return _fetched()
        assert operation == "observe"
        assert query is True
        return _status("failed", errorClass="watchdog_timeout", errorMessage="worker expired")

    monkeypatch.setattr(client, "_content_pipeline_call", request)

    with pytest.raises(BibiContentTaskError, match="worker expired") as error:
        await client.summarize(_VIDEO_URL)

    assert error.value.content_id == _CONTENT_ID
    assert len(fetch_payloads) == 3
    assert fetch_payloads[0]["forceFresh"] is False
    assert all(payload["forceFresh"] is True for payload in fetch_payloads[1:])
    assert all(payload["contentId"] == _CONTENT_ID for payload in fetch_payloads[1:])


async def test_watchdog_retry_does_not_restart_total_wait_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, bibigpt_web_queue_wait_seconds=0.4)
    fetch_count = 0
    second_fetch_cancelled = asyncio.Event()

    async def request(
        operation: str, payload: dict[str, Any], *, query: bool = False
    ) -> dict[str, Any]:
        nonlocal fetch_count
        if operation == "fetch":
            fetch_count += 1
            if fetch_count == 1:
                await asyncio.sleep(0.3)
                return _fetched()
            try:
                await asyncio.Event().wait()
            finally:
                second_fetch_cancelled.set()
        return _status("failed", errorClass="watchdog_timeout", errorMessage="worker expired")

    monkeypatch.setattr(client, "_content_pipeline_call", request)

    async with asyncio.timeout(0.6):
        with pytest.raises(BibiContentPendingError) as error:
            await client._prepare_content(_VIDEO_URL)

    assert fetch_count == 2
    assert second_fetch_cancelled.is_set()
    assert error.value.content_id == _CONTENT_ID
    assert error.value.stage == "fetch"
    assert error.value.last_error == "worker expired"


async def test_wait_timeout_cancels_a_hanging_observation_and_keeps_last_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, bibigpt_web_queue_wait_seconds=0.06)
    observe_count = 0
    hung_request_cancelled = asyncio.Event()

    async def request(
        operation: str, payload: dict[str, Any], *, query: bool = False
    ) -> dict[str, Any]:
        nonlocal observe_count
        if operation == "fetch":
            return _fetched()
        assert operation == "observe"
        observe_count += 1
        if observe_count == 1:
            raise BibiAPIError(503, "status transport unavailable")
        try:
            await asyncio.Event().wait()
        finally:
            hung_request_cancelled.set()
        raise AssertionError("a cancelled request cannot finish")

    monkeypatch.setattr(client, "_content_pipeline_call", request)

    with pytest.raises(BibiContentPendingError) as error:
        await client._prepare_content(_VIDEO_URL)

    assert observe_count == 2
    assert hung_request_cancelled.is_set()
    assert error.value.content_id == _CONTENT_ID
    assert error.value.stage == "observe"
    assert "status transport unavailable" in error.value.last_error


async def test_external_cancellation_is_not_reported_as_upstream_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path)
    observation_started = asyncio.Event()

    async def request(
        operation: str, payload: dict[str, Any], *, query: bool = False
    ) -> dict[str, Any]:
        if operation == "fetch":
            return _fetched()
        observation_started.set()
        await asyncio.Event().wait()
        raise AssertionError("a cancelled request cannot finish")

    monkeypatch.setattr(client, "_content_pipeline_call", request)
    task = asyncio.create_task(client.summarize(_VIDEO_URL))
    await observation_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("initial_error", [BibiAPIError(524, "edge timeout"), ValueError("empty")])
async def test_later_risk_control_upgrades_recovery_to_content_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initial_error: Exception
) -> None:
    client = _client(tmp_path)
    direct = AsyncMock(side_effect=[initial_error, BibiAPIError(500, "平台风控，稍后再试")])
    pipeline = AsyncMock(return_value=_result())
    monkeypatch.setattr(client, "_summarize_once", direct)
    monkeypatch.setattr(client, "_summarize_content_pipeline", pipeline)
    monkeypatch.setattr("src.bibi_client._RECOVERY_DELAYS", (0, 0))

    result = await client._summarize_with_recovery(_VIDEO_URL, "original prompt", refresh=True)

    assert result.content_id == _CONTENT_ID
    assert [call.kwargs["refresh"] for call in direct.await_args_list] == [True, False]
    pipeline.assert_awaited_once_with(_VIDEO_URL, "original prompt")


@pytest.mark.parametrize(
    "access_mode,enabled,video_url",
    [
        ("web", True, _VIDEO_URL),
        ("browser", False, _VIDEO_URL),
        ("browser", True, "https://www.youtube.com/watch?v=example"),
    ],
)
async def test_pipeline_opt_in_preserves_other_entry_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    access_mode: str,
    enabled: bool,
    video_url: str,
) -> None:
    client = _client(
        tmp_path, bibigpt_access_mode=access_mode, bibigpt_web_queue_enabled=enabled
    )
    direct = AsyncMock(return_value=_result())
    pipeline = AsyncMock(side_effect=AssertionError("pipeline must not run"))
    monkeypatch.setattr(client, "_summarize_with_recovery", direct)
    monkeypatch.setattr(client, "_summarize_content_pipeline", pipeline)

    await client.summarize(video_url)

    direct.assert_awaited_once_with(video_url, "", refresh=True)
    pipeline.assert_not_awaited()


@pytest.mark.parametrize("summary", [None, "", " \n ", {"text": "wrong type"}])
async def test_pipeline_rejects_missing_or_invalid_summary_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, summary: Any
) -> None:
    client = _client(tmp_path)
    request = AsyncMock(side_effect=[_fetched(), _status("ready"), {"summaryText": summary}])
    monkeypatch.setattr(client, "_content_pipeline_call", request)

    with pytest.raises(BibiAPIError, match="summaryText"):
        await client.summarize(_VIDEO_URL)

    assert [call.args[0] for call in request.await_args_list] == ["fetch", "observe", "summarize"]


@pytest.mark.parametrize("response", [{}, {"detail": {}}, {"detail": {"dbId": " "}}])
async def test_pipeline_does_not_accept_client_side_task_id_as_server_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response: dict[str, Any]
) -> None:
    client = _client(tmp_path)
    request = AsyncMock(return_value={"taskId": "task_local_only", **response})
    legacy = AsyncMock(
        return_value={"result": {"data": {"json": {"taskId": "task_local_only"}}}}
    )
    monkeypatch.setattr(client, "_content_pipeline_call", request)
    monkeypatch.setattr(client, "_browser_fetch_json", legacy)

    with pytest.raises(BibiAPIError, match="server content ID"):
        await client.summarize(_VIDEO_URL)

    request.assert_awaited_once()
    legacy.assert_awaited_once()


@pytest.mark.parametrize(
    "first_error",
    [
        BibiAPIError(524, "edge timeout"),
        BibiTimeoutError(0, "browser request timeout"),
        BibiAPIError(500, "model provider temporarily unavailable"),
        BibiAPIError(502, "bad gateway"),
        BibiAPIError(503, "service unavailable"),
    ],
)
async def test_prepared_summary_recovers_without_restarting_subtitle_processing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_error: BibiAPIError
) -> None:
    client = _client(tmp_path)
    request = AsyncMock(
        side_effect=[_fetched(), _status("ready"), first_error, {"summaryText": "- recovered"}]
    )
    monkeypatch.setattr(client, "_content_pipeline_call", request)
    monkeypatch.setattr("src.bibi_client._RECOVERY_DELAYS", (0, 0, 0))

    result = await client.summarize(_VIDEO_URL, prompt="Keep all examples")

    assert result.content == "- recovered"
    assert result.content_id == _CONTENT_ID
    assert [call.args[0] for call in request.await_args_list] == [
        "fetch", "observe", "summarize", "summarize"
    ]
    initial, recovery = [call.args[1] for call in request.await_args_list[2:]]
    assert initial["promptConfig"]["isRefresh"] is True
    assert recovery["promptConfig"]["isRefresh"] is False
    assert recovery == {
        **initial,
        "promptConfig": {**initial["promptConfig"], "isRefresh": False},
    }
    assert recovery["promptConfig"]["model"] == "configured-model"
    assert recovery["promptConfig"]["customPrompt"].startswith("Keep all examples")


async def test_real_browser_summary_timeout_cancels_request_then_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, bibigpt_browser_timeout=0.02)
    calls: list[tuple[str, dict[str, Any] | None]] = []
    summary_requests = 0
    timed_out_request_cancelled = asyncio.Event()

    @asynccontextmanager
    async def page_context() -> AsyncIterator[object]:
        yield object()

    async def browser_request(
        page: object, url: str, body: dict[str, Any] | None, *, method: str = "POST"
    ) -> list[dict[str, Any]]:
        nonlocal summary_requests
        operation = urlsplit(url).path.rsplit(".", 1)[-1]
        calls.append((operation, body))
        if operation == "fetch":
            data = _fetched()
        elif operation == "observe":
            assert method == "GET"
            data = _status("ready")
        else:
            assert operation == "summarize"
            summary_requests += 1
            if summary_requests == 1:
                try:
                    await asyncio.Event().wait()
                finally:
                    timed_out_request_cancelled.set()
            data = {"summaryText": "- browser recovery"}
        return [{"result": {"data": {"json": data}}}]

    monkeypatch.setattr(client, "_browser_page", page_context)
    monkeypatch.setattr(client, "_browser_request_json", browser_request)
    monkeypatch.setattr("src.bibi_client._RECOVERY_DELAYS", (0, 0, 0))

    result = await client.summarize(_VIDEO_URL)

    assert result.content == "- browser recovery"
    assert timed_out_request_cancelled.is_set()
    assert [call[0] for call in calls] == ["fetch", "observe", "summarize", "summarize"]
    bodies = [body for operation, body in calls if operation == "summarize"]
    assert [body["0"]["json"]["promptConfig"]["isRefresh"] for body in bodies] == [True, False]


async def test_repeated_summary_risk_control_exhausts_only_summary_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path)
    errors = [BibiAPIError(500, f"平台风控，attempt {attempt}") for attempt in range(1, 5)]
    request = AsyncMock(side_effect=[_fetched(), _status("ready"), *errors])
    monkeypatch.setattr(client, "_content_pipeline_call", request)
    monkeypatch.setattr("src.bibi_client._RECOVERY_DELAYS", (0, 0, 0))

    with pytest.raises(BibiContentPendingError) as error:
        await client.summarize(_VIDEO_URL)

    assert [call.args[0] for call in request.await_args_list] == [
        "fetch", "observe", "summarize", "summarize", "summarize", "summarize"
    ]
    configs = [call.args[1]["promptConfig"] for call in request.await_args_list[2:]]
    assert [config["isRefresh"] for config in configs] == [True, False, False, False]
    assert error.value.content_id == _CONTENT_ID
    assert error.value.stage == "summarize"
    assert error.value.last_error == str(errors[-1])
    assert error.value.__cause__ is errors[-1]
    assert not isinstance(error.value, BibiContentTaskError)


@pytest.mark.parametrize("status_code", [401, 402, 403, 429])
async def test_nonrecoverable_prepared_summary_error_propagates_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    client = _client(tmp_path)
    error_type = AuthenticationError if status_code in (401, 403) else BibiAPIError
    upstream_error = error_type(status_code, "account or quota requirement")
    request = AsyncMock(side_effect=[_fetched(), _status("ready"), upstream_error])
    monkeypatch.setattr(client, "_content_pipeline_call", request)
    monkeypatch.setattr("src.bibi_client._RECOVERY_DELAYS", (0, 0, 0))

    with pytest.raises(error_type) as error:
        await client.summarize(_VIDEO_URL)

    assert error.value is upstream_error
    assert [call.args[0] for call in request.await_args_list] == ["fetch", "observe", "summarize"]


@pytest.mark.parametrize(
    "pipeline_outcome",
    [
        BibiAPIError(500, "平台风控，稍后再试"),
        BibiAPIError(524, "edge timeout"),
        BibiTimeoutError(0, "request timed out"),
        {},
        {"detail": {}},
        {"detail": {"dbId": " "}},
        {"detail": {"dbId": 123}},
    ],
)
@pytest.mark.parametrize("target,force_fresh", [("subtitle", False), ("metadata", True)])
async def test_fetch_falls_back_once_to_the_webpage_content_info_protocol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pipeline_outcome: Exception | dict[str, Any],
    target: str,
    force_fresh: bool,
) -> None:
    client = _client(tmp_path)
    pipeline = AsyncMock(
        **(
            {"side_effect": pipeline_outcome}
            if isinstance(pipeline_outcome, Exception)
            else {"return_value": pipeline_outcome}
        )
    )
    detail = {"dbId": _CONTENT_ID, "subtitlesArray": []}
    legacy = AsyncMock(return_value={"result": {"data": {"json": detail}}})
    monkeypatch.setattr(client, "_content_pipeline_call", pipeline)
    monkeypatch.setattr(client, "_browser_fetch_json", legacy)
    payload = {
        "url": _VIDEO_URL,
        "target": target,
        "forceFresh": force_fresh,
        "audioConfig": {"audioLanguage": "zh", "transcribeProvider": "auto"},
        "includeDetail": True,
        **({"contentId": _CONTENT_ID} if force_fresh else {}),
    }

    actual, via_pipeline = await client._fetch_content_detail(payload)

    assert actual == detail
    assert via_pipeline is False
    pipeline.assert_awaited_once_with("fetch", payload)
    legacy.assert_awaited_once()
    args, kwargs = legacy.await_args
    url = urlsplit(args[0])
    assert url.netloc == "aitodo.co"
    assert url.path == "/api/trpc/content.info"
    assert args[1] is None
    assert kwargs == {"method": "GET"}
    assert json.loads(parse_qs(url.query)["input"][0]) == {
        "json": {
            "url": _VIDEO_URL,
            "contentId": _CONTENT_ID if force_fresh else "",
            "audioConfig": payload["audioConfig"],
            "skipSubtitleTask": target == "metadata",
            "isRefresh": force_fresh,
        }
    }
    assert "batch" not in parse_qs(url.query)


@pytest.mark.parametrize("status_code", [400, 401, 402, 403, 429])
async def test_fetch_does_not_retry_account_quota_or_invalid_request_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    client = _client(tmp_path)
    error_type = AuthenticationError if status_code in (401, 403) else BibiAPIError
    upstream_error = error_type(status_code, "account quota or input requirement")
    pipeline = AsyncMock(side_effect=upstream_error)
    legacy = AsyncMock(side_effect=AssertionError("nonrecoverable fetch must stop"))
    monkeypatch.setattr(client, "_content_pipeline_call", pipeline)
    monkeypatch.setattr(client, "_browser_fetch_json", legacy)

    with pytest.raises(error_type) as error:
        await client._prepare_content(_VIDEO_URL)

    assert error.value is upstream_error
    pipeline.assert_awaited_once()
    legacy.assert_not_awaited()


@pytest.mark.parametrize(
    "malformed_response",
    [None, [], "not a tRPC response", {"result": {"data": {"json": None}}}],
)
async def test_fetch_invalid_trpc_projection_uses_legacy_compatibility_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, malformed_response: Any
) -> None:
    client = _client(tmp_path)
    wire = AsyncMock(side_effect=[
        malformed_response,
        {"result": {"data": {"json": {"dbId": _CONTENT_ID}}}},
        [{"result": {"data": {"json": _status("ready")}}}],
    ])
    monkeypatch.setattr(client, "_browser_fetch_json", wire)

    prepared = await client._prepare_content(_VIDEO_URL)

    assert prepared.content_id == _CONTENT_ID
    assert prepared.summary_via_pipeline is False
    assert [urlsplit(call.args[0]).path.rsplit("/", 1)[-1] for call in wire.await_args_list] == [
        "contentPipeline.fetch", "content.info", "contentPipeline.observe",
    ]


async def test_legacy_fetch_waits_for_pipeline_ready_then_uses_legacy_summary_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path)
    calls: list[str] = []
    statuses = iter(["pending", "running", "ready"])
    config: dict[str, Any] = {}

    async def request(
        url: str, body: dict[str, Any] | None, *, method: str = "POST"
    ) -> list[dict[str, Any]] | dict[str, Any]:
        operation = urlsplit(url).path.rsplit("/", 1)[-1]
        calls.append(operation)
        if operation == "contentPipeline.fetch":
            raise BibiAPIError(500, "平台风控，稍后再试")
        if operation == "content.info":
            assert method == "GET"
            return {"result": {"data": {"json": {"dbId": _CONTENT_ID}}}}
        if operation == "contentPipeline.observe":
            assert method == "GET"
            assert json.loads(parse_qs(urlsplit(url).query)["input"][0]) == {
                "json": {"contentId": _CONTENT_ID}
            }
            return [{"result": {"data": {"json": _status(next(statuses))}}}]
        assert operation == "video.summaryBySetting"
        assert method == "POST"
        assert calls[-4:-1] == ["contentPipeline.observe"] * 3
        assert body is not None
        config.update(body["0"]["json"]["promptConfig"])
        return [{"result": {"data": {"json": {
            "summary": "- legacy completed", "contentId": "a-different-summary-id"
        }}}}]

    forbidden = AsyncMock(side_effect=AssertionError("must not enter recursive recovery"))
    monkeypatch.setattr(client, "_browser_fetch_json", request)
    monkeypatch.setattr(client, "_summarize_with_recovery", forbidden)
    monkeypatch.setattr(client, "_recover_via_lookup", forbidden)

    result = await client.summarize(_VIDEO_URL, prompt="Preserve each technical example")

    assert result.content == "- legacy completed"
    assert result.content_id == _CONTENT_ID
    assert calls == [
        "contentPipeline.fetch", "content.info", "contentPipeline.observe",
        "contentPipeline.observe", "contentPipeline.observe", "video.summaryBySetting",
    ]
    assert config["model"] == "configured-model"
    assert config["customPrompt"].startswith("Preserve each technical example")
    assert config["isRefresh"] is True
    forbidden.assert_not_awaited()


@pytest.mark.parametrize("eventual_success", [True, False])
async def test_legacy_summary_recovery_never_resubmits_subtitle_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, eventual_success: bool
) -> None:
    client = _client(tmp_path)
    request = AsyncMock(side_effect=[BibiAPIError(500, "平台风控 fetch"), _status("ready")])
    legacy_fetch = AsyncMock(
        return_value={"result": {"data": {"json": {"dbId": _CONTENT_ID}}}}
    )
    errors = [BibiAPIError(500, f"平台风控 summary {attempt}") for attempt in range(1, 5)]
    legacy_summary = AsyncMock(
        side_effect=[*errors[:3], _result()] if eventual_success else errors
    )
    recursive = AsyncMock(side_effect=AssertionError("must not recursively restart pipeline"))
    monkeypatch.setattr(client, "_content_pipeline_call", request)
    monkeypatch.setattr(client, "_browser_fetch_json", legacy_fetch)
    monkeypatch.setattr(client, "_summarize_browser", legacy_summary)
    monkeypatch.setattr(client, "_summarize_with_recovery", recursive)
    monkeypatch.setattr(client, "_recover_via_lookup", recursive)
    monkeypatch.setattr("src.bibi_client._RECOVERY_DELAYS", (0, 0, 0))

    if eventual_success:
        result = await client.summarize(_VIDEO_URL, prompt="Original prompt")
        assert result.content_id == _CONTENT_ID
    else:
        with pytest.raises(BibiContentPendingError) as error:
            await client.summarize(_VIDEO_URL, prompt="Original prompt")
        assert error.value.content_id == _CONTENT_ID
        assert error.value.stage == "summarize"
        assert error.value.last_error == str(errors[-1])
        assert error.value.__cause__ is errors[-1]

    assert [call.args[0] for call in request.await_args_list] == ["fetch", "observe"]
    legacy_fetch.assert_awaited_once()
    assert len(legacy_summary.await_args_list) == 4
    assert [call.kwargs["refresh"] for call in legacy_summary.await_args_list] == [
        True, False, False, False
    ]
    assert all(
        call.args[0] == _VIDEO_URL and call.args[1].startswith("Original prompt")
        for call in legacy_summary.await_args_list
    )
    recursive.assert_not_awaited()


async def test_both_content_fetch_routes_failing_preserves_final_upstream_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path)
    final_error = BibiAPIError(503, "legacy extractor unavailable")
    pipeline = AsyncMock(side_effect=BibiAPIError(500, "平台风控"))
    legacy = AsyncMock(side_effect=final_error)
    monkeypatch.setattr(client, "_content_pipeline_call", pipeline)
    monkeypatch.setattr(client, "_browser_fetch_json", legacy)

    with pytest.raises(BibiAPIError) as error:
        await client.summarize(_VIDEO_URL)

    assert error.value is final_error
    pipeline.assert_awaited_once()
    legacy.assert_awaited_once()


async def test_hanging_legacy_fetch_uses_the_same_total_wait_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, bibigpt_web_queue_wait_seconds=0.4)
    legacy_cancelled = asyncio.Event()

    async def pipeline(*args: Any, **kwargs: Any) -> dict[str, Any]:
        await asyncio.sleep(0.3)
        raise BibiAPIError(500, "平台风控 before legacy fallback")

    async def legacy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            await asyncio.Event().wait()
        finally:
            legacy_cancelled.set()
        raise AssertionError("cancelled legacy fetch cannot finish")

    monkeypatch.setattr(client, "_content_pipeline_call", pipeline)
    monkeypatch.setattr(client, "_browser_fetch_json", legacy)

    async with asyncio.timeout(0.6):
        with pytest.raises(BibiContentPendingError) as error:
            await client._prepare_content(_VIDEO_URL)

    assert legacy_cancelled.is_set()
    assert error.value.content_id == ""
    assert error.value.stage == "fetch"


async def test_external_cancellation_stops_legacy_fetch_without_further_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path)
    legacy_started = asyncio.Event()
    pipeline = AsyncMock(side_effect=BibiAPIError(500, "平台风控"))

    async def legacy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        legacy_started.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled legacy fetch cannot finish")

    legacy_request = AsyncMock(side_effect=legacy)
    monkeypatch.setattr(client, "_content_pipeline_call", pipeline)
    monkeypatch.setattr(client, "_browser_fetch_json", legacy_request)
    task = asyncio.create_task(client.summarize(_VIDEO_URL))
    await legacy_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    pipeline.assert_awaited_once()
    legacy_request.assert_awaited_once()
