import asyncio
import json
import logging
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import lark_oapi as lark

from src.archive_store import (
    ArchiveEntry,
    BitableArchive,
    ChatDirectory,
    _decode_row,
)
from src.config import Settings


def _api_response(data: dict, success: bool = True):
    payload = json.dumps({"code": 0 if success else 999, "data": data}).encode("utf-8")
    return SimpleNamespace(
        success=lambda: success,
        code=0 if success else 999,
        msg="ok" if success else "failed",
        raw=SimpleNamespace(content=payload),
    )


def _client(*responses):
    return SimpleNamespace(arequest=AsyncMock(side_effect=list(responses)))


def _settings() -> Settings:
    return Settings(bitable_app_token="app_x", bitable_table_id="tbl_x")


def _entry(**overrides) -> ArchiveEntry:
    defaults = dict(
        title="译名",
        url="https://youtu.be/abc",
        platform="youtube",
        channel="Some Channel",
        duration_seconds=90,
        sender_open_id="ou_sender",
        chat_id="oc_chat",
        chat_type="group",
        recorded_at_utc=datetime(2026, 7, 21, 14, 30, tzinfo=UTC),
    )
    defaults.update(overrides)
    return ArchiveEntry(**defaults)


def _archive(client) -> BitableArchive:
    return BitableArchive(_settings(), client, ChatDirectory(client))


def test_encode_fields_shapes() -> None:
    archive = _archive(_client())
    entry = _entry(chat_name="链接群")

    fields = archive._encode_fields(entry)

    assert fields["标题"] == "译名"
    assert fields["链接"] == {"link": "https://youtu.be/abc", "text": "https://youtu.be/abc"}
    assert fields["平台"] == "youtube"
    assert fields["时长"] == "1:30"
    assert fields["发送人"] == [{"id": "ou_sender"}]
    assert fields["发送的群"] == "链接群"
    # 2026-07-21 14:30 UTC == 22:30 Beijing, stored as epoch ms for the
    # bitable DateTime field.
    assert fields["记录时间"] == 1784644200000


def test_encode_fields_fallbacks() -> None:
    archive = _archive(_client())
    entry = _entry(title="", duration_seconds=None)

    fields = archive._encode_fields(entry)

    assert fields["标题"] == "https://youtu.be/abc"
    assert fields["时长"] == ""
    assert fields["发送人"] == [{"id": "ou_sender"}]
    assert fields["发送的群"] == "oc_chat"


async def test_process_one_creates_record_with_resolved_chat_name() -> None:
    client = _client(
        _api_response({"name": "链接群"}),
        _api_response({"items": [], "has_more": False}),
        _api_response({"record": {"record_id": "rec1"}}),
    )
    archive = _archive(client)

    await archive._process_one(_entry())

    create_request = client.arequest.call_args_list[2].args[0]
    assert create_request.uri == "/open-apis/bitable/v1/apps/app_x/tables/tbl_x/records"
    assert create_request.body["fields"]["发送人"] == [{"id": "ou_sender"}]
    assert create_request.body["fields"]["发送的群"] == "链接群"


async def test_process_one_deletes_stale_duplicate_after_creating_new_row() -> None:
    client = _client(
        _api_response({"name": "链接群"}),
        _api_response(
            {
                "items": [
                    {
                        "record_id": "rec_old",
                        "fields": {"链接": {"link": "https://youtu.be/abc?si=track"}},
                    },
                    {
                        "record_id": "rec_other",
                        "fields": {"链接": {"link": "https://youtu.be/xyz"}},
                    },
                ],
                "has_more": False,
            }
        ),
        _api_response({"record": {"record_id": "rec_new"}}),
        _api_response({"deleted": True, "record_id": "rec_old"}),
    )
    archive = _archive(client)

    await archive._process_one(_entry())

    calls = client.arequest.call_args_list
    create_request = calls[2].args[0]
    delete_request = calls[3].args[0]
    assert create_request.uri == "/open-apis/bitable/v1/apps/app_x/tables/tbl_x/records"
    assert delete_request.uri == "/open-apis/bitable/v1/apps/app_x/tables/tbl_x/records/rec_old"
    assert delete_request.http_method == lark.HttpMethod.DELETE


async def test_process_one_no_duplicate_skips_delete() -> None:
    client = _client(
        _api_response({"name": "链接群"}),
        _api_response({"items": [], "has_more": False}),
        _api_response({"record": {"record_id": "rec_new"}}),
    )
    archive = _archive(client)

    await archive._process_one(_entry())

    assert client.arequest.await_count == 3


async def test_delete_record_failure_logs_warning_and_does_not_raise(caplog) -> None:
    client = SimpleNamespace(arequest=AsyncMock(side_effect=RuntimeError("boom")))
    archive = _archive(client)

    with caplog.at_level(logging.WARNING):
        await archive._delete_record("rec_old")

    assert any("failed to delete stale duplicate row" in r.message for r in caplog.records)


async def test_run_drops_failed_entries_with_warning(caplog) -> None:
    archive = _archive(_client())
    archive._process_one = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    archive.enqueue(_entry())

    with caplog.at_level(logging.WARNING):
        task = asyncio.create_task(archive.run())
        await asyncio.sleep(0.05)
        task.cancel()

    assert any("row dropped" in r.message for r in caplog.records)


async def test_fetch_day_paginates_and_filters_client_side() -> None:
    # Epoch ms for 2026-07-21 09:00, 2026-07-20 23:59 and 2026-07-21 00:00 Beijing.
    page1 = {
        "items": [
            {
                "fields": {
                    "标题": [{"text": "视频A", "type": "text"}],
                    "链接": {"link": "https://a", "text": "https://a"},
                    "平台": "youtube",
                    "频道": "ChanA",
                    "时长": "3:05",
                    "发送人": "张三",
                    "发送的群": "链接群",
                    "记录时间": 1784595600000,
                }
            }
        ],
        "has_more": True,
        "page_token": "tok2",
    }
    page2 = {
        "items": [
            {
                "fields": {
                    "标题": "视频B",
                    "链接": {"link": "https://b"},
                    "平台": "bilibili",
                    "记录时间": 1784563140000,
                }
            },
            {
                "fields": {
                    "标题": "视频C",
                    "链接": {"link": "https://c"},
                    "平台": "x",
                    "记录时间": 1784563200000,
                }
            },
        ],
        "has_more": False,
    }
    client = _client(_api_response(page1), _api_response(page2))
    archive = _archive(client)

    rows = await archive.fetch_day(date(2026, 7, 21))

    assert [r.title for r in rows] == ["视频C", "视频A"]
    assert rows[1].url == "https://a"
    assert rows[1].duration == "3:05"
    first_request = client.arequest.call_args_list[0].args[0]
    assert "filter" not in first_request.body


def test_decode_row_handles_segment_lists_epoch_ms_and_legacy_text() -> None:
    row = _decode_row(
        {
            "标题": [{"text": "分段", "type": "text"}, {"text": "标题", "type": "text"}],
            "链接": {"link": "https://x"},
            "记录时间": 1784595600000,
        }
    )

    assert row.title == "分段标题"
    assert row.url == "https://x"
    assert row.recorded_at == "2026-07-21 09:00"


def test_decode_row_reads_sender_from_people_field() -> None:
    row = _decode_row({"发送人": [{"id": "ou_sender", "name": "张三", "en_name": "Zhang San"}]})
    assert row.sender == "张三"

    legacy_row = _decode_row({"发送人": "张三"})
    assert legacy_row.sender == "张三"

    legacy_row = _decode_row({"记录时间": "2026-07-21 10:00"})
    assert legacy_row.recorded_at == "2026-07-21 10:00"


async def test_chat_directory_caches_chat_name_lookup() -> None:
    client = _client(
        _api_response({"name": "链接群"}),
    )
    directory = ChatDirectory(client)

    assert await directory.chat_name("oc_chat", "group") == "链接群"
    assert await directory.chat_name("oc_chat", "group") == "链接群"
    assert client.arequest.await_count == 1


async def test_chat_directory_p2p_and_degradation(caplog) -> None:
    client = SimpleNamespace(arequest=AsyncMock(side_effect=RuntimeError("no perm")))
    directory = ChatDirectory(client)

    assert await directory.chat_name("oc_p2p", "p2p") == "私聊"
    with caplog.at_level(logging.WARNING):
        assert await directory.chat_name("oc_chat", "group") == "oc_chat"
        assert await directory.chat_name("oc_chat", "group") == "oc_chat"

    warnings = [r for r in caplog.records if "resolve chat name" in r.message]
    assert len(warnings) == 1
