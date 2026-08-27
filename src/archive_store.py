from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import lark_oapi as lark
import tenacity

from .card import fmt_duration
from .config import Settings
from .platforms import normalize_url
from .time_utils import format_beijing, now_utc

logger = logging.getLogger(__name__)

# Logical name -> (display name, bitable field type). Order matters: the first
# entry becomes the table's primary column at bootstrap.
_TEXT = 1
_SINGLE_SELECT = 3
_URL = 15
_DATE_TIME = 5
_USER = 11
_FIELDS: tuple[tuple[str, str, int], ...] = (
    ("title", "标题", _TEXT),
    ("url", "链接", _URL),
    ("platform", "平台", _SINGLE_SELECT),
    ("channel", "频道", _TEXT),
    ("duration", "时长", _TEXT),
    ("sender", "发送人", _USER),
    ("chat", "发送的群", _SINGLE_SELECT),
    ("recorded_at", "记录时间", _DATE_TIME),
    ("remark", "备注", _TEXT),
    ("bibigpt_url", "BibiGPT 链接", _URL),
)
_DISPLAY = {logical: display for logical, display, _ in _FIELDS}

# Human-friendly Beijing time; zero-padded so lexical order equals time order
# and the daily report can filter by date prefix.
_RECORDED_AT_FMT = "%Y-%m-%d %H:%M"
# Remark lines are read inside a single table cell, so a compact month-day
# prefix is enough to tell replies apart.
_REMARK_AT_FMT = "%m-%d %H:%M"


class ArchiveError(Exception):
    pass


@dataclass
class ArchiveEntry:
    title: str
    url: str
    platform: str
    channel: str
    duration_seconds: int | None
    sender_open_id: str
    chat_id: str
    chat_type: str
    recorded_at_utc: datetime = field(default_factory=now_utc)
    # Resolved by the consumer coroutine, never by the send hot path.
    chat_name: str = ""


@dataclass
class RemarkAppend:
    """Append one reply line to the "备注" cell of the archived row for url.
    The sender name is resolved by the consumer coroutine, like chat names."""

    url: str
    text: str
    sender_open_id: str
    chat_id: str
    chat_type: str
    replied_at_utc: datetime = field(default_factory=now_utc)


@dataclass
class BibiLinkUpdate:
    """Set the "BibiGPT 链接" cell of the archived row for url (idempotent)."""

    url: str
    bibigpt_url: str


ArchiveTask = ArchiveEntry | RemarkAppend | BibiLinkUpdate


@dataclass
class ArchivedRow:
    """A row read back from the bitable table (all values as displayed)."""

    title: str
    url: str
    platform: str
    channel: str
    duration: str
    sender: str
    chat: str
    recorded_at: str
    remark: str = ""
    bibigpt_url: str = ""


async def _bitable_request(
    client: lark.Client,
    method: lark.HttpMethod,
    uri: str,
    body: dict | None = None,
    queries: list[tuple[str, str]] | None = None,
) -> dict:
    builder = (
        lark.BaseRequest.builder()
        .http_method(method)
        .uri(uri)
        .token_types({lark.AccessTokenType.TENANT})
    )
    if body is not None:
        builder = builder.body(body)
    if queries:
        builder = builder.queries(queries)
    resp = await client.arequest(builder.build())
    if not resp.success():
        raise ArchiveError(f"{method.name} {uri} failed: code={resp.code} msg={resp.msg}")
    return json.loads(resp.raw.content.decode("utf-8")).get("data") or {}


class ChatDirectory:
    """Resolves chat_id to a human-readable group name via the chats the bot
    is already in (no contact-scope needed). Failures degrade to the raw id
    with a single warning per id; results are cached for the process lifetime.
    Sender identity is no longer resolved here: the archive's "发送人" column
    is a bitable People field keyed by open_id, resolved to a display name by
    Feishu itself.
    """

    def __init__(self, client: lark.Client) -> None:
        self._client = client
        self._chat_names: dict[str, str] = {}
        self._member_names: dict[tuple[str, str], str] = {}
        self._warned: set[str] = set()

    async def chat_name(self, chat_id: str, chat_type: str) -> str:
        if chat_type == "p2p":
            return "私聊"
        cached = self._chat_names.get(chat_id)
        if cached is not None:
            return cached
        try:
            data = await _bitable_request(
                self._client, lark.HttpMethod.GET, f"/open-apis/im/v1/chats/{chat_id}"
            )
            name = str(data.get("name") or "") or chat_id
        except Exception as e:
            self._warn_once(chat_id, f"failed to resolve chat name: chat_id={chat_id} error={e}")
            return chat_id
        self._chat_names[chat_id] = name
        return name

    async def member_name(self, chat_id: str, chat_type: str, open_id: str) -> str:
        """Resolve a member open_id to a display name for the remark column
        (plain text, so Feishu does no server-side resolution there). Uses the
        chat member list (im:chat:readonly, the scope already held for chat
        names) and caches the whole roster per chat. p2p chats have no member
        list; those and any failure degrade to the raw open_id."""
        if chat_type == "p2p":
            return open_id
        cached = self._member_names.get((chat_id, open_id))
        if cached is not None:
            return cached
        try:
            page_token = ""
            while True:
                queries: list[tuple[str, str]] = [
                    ("member_id_type", "open_id"),
                    ("page_size", "100"),
                ]
                if page_token:
                    queries.append(("page_token", page_token))
                data = await _bitable_request(
                    self._client,
                    lark.HttpMethod.GET,
                    f"/open-apis/im/v1/chats/{chat_id}/members",
                    queries=queries,
                )
                for item in data.get("items") or []:
                    member_id = str(item.get("member_id") or "")
                    name = str(item.get("name") or "")
                    if member_id and name:
                        self._member_names[(chat_id, member_id)] = name
                page_token = str(data.get("page_token") or "")
                if not data.get("has_more") or not page_token:
                    break
        except Exception as e:
            self._warn_once(
                f"members:{chat_id}",
                f"failed to resolve chat members: chat_id={chat_id} error={e}",
            )
            return open_id
        return self._member_names.get((chat_id, open_id)) or open_id

    def _warn_once(self, key: str, message: str) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        logger.warning(message)


class BitableArchive:
    """Mirrors successfully sent link cards into a Feishu bitable table.

    Writes go through an in-memory queue drained by a single consumer so the
    card-send hot path never awaits bitable, and rows are created serially.
    Failures are logged and dropped: the bitable is a best-effort mirror.
    """

    def __init__(self, settings: Settings, client: lark.Client, directory: ChatDirectory) -> None:
        self._settings = settings
        self._client = client
        self._directory = directory
        self._queue: asyncio.Queue[ArchiveTask] = asyncio.Queue()

    def enqueue(self, task: ArchiveTask) -> None:
        self._queue.put_nowait(task)

    async def run(self) -> None:
        logger.info("bitable archive consumer started")
        while True:
            task = await self._queue.get()
            try:
                if isinstance(task, ArchiveEntry):
                    await self._process_one(task)
                elif isinstance(task, RemarkAppend):
                    await self._append_remark(task)
                else:
                    await self._set_bibigpt_url(task)
            except Exception as e:
                logger.warning(
                    "bitable archive %s failed, dropped: url=%s error=%s",
                    task.__class__.__name__,
                    task.url,
                    e,
                )

    async def _process_one(self, entry: ArchiveEntry) -> None:
        entry.chat_name = await self._directory.chat_name(entry.chat_id, entry.chat_type)
        normalized = normalize_url(entry.url)
        stale = await self._find_records_by_normalized_url(normalized)
        fields = self._encode_fields(entry)
        # A fresh entry never carries remark/BibiGPT-link values of its own,
        # so re-sharing a link must inherit them from the row it replaces.
        fields.update(_carried_over_fields(stale))
        # Create the new row before deleting the stale one(s): if create fails
        # and raises, the old row is still there instead of both being gone.
        await self._create_record(fields)
        for record_id, _ in stale:
            await self._delete_record(record_id)
        logger.info(
            "archived: url=%s platform=%s chat=%s", entry.url, entry.platform, entry.chat_name
        )

    async def _append_remark(self, task: RemarkAppend) -> None:
        found = await self._find_records_by_normalized_url(normalize_url(task.url))
        if not found:
            logger.warning("remark dropped, no archived row for url=%s", task.url)
            return
        record_id, fields = found[0]
        name = await self._directory.member_name(task.chat_id, task.chat_type, task.sender_open_id)
        stamp = format_beijing(task.replied_at_utc, _REMARK_AT_FMT)
        line = f"[{stamp}] {name}: {task.text}"
        existing = _flatten_value(fields.get(_DISPLAY["remark"]))
        value = f"{existing}\n{line}" if existing else line
        await self._update_record(record_id, {_DISPLAY["remark"]: value})
        logger.info("remark appended: url=%s record_id=%s", task.url, record_id)

    async def _set_bibigpt_url(self, task: BibiLinkUpdate) -> None:
        found = await self._find_records_by_normalized_url(normalize_url(task.url))
        if not found:
            logger.warning("bibigpt link dropped, no archived row for url=%s", task.url)
            return
        record_id, _ = found[0]
        await self._update_record(
            record_id,
            {_DISPLAY["bibigpt_url"]: {"link": task.bibigpt_url, "text": task.bibigpt_url}},
        )
        logger.info("bibigpt link set: url=%s record_id=%s", task.url, record_id)

    async def _create_record(self, fields: dict[str, object]) -> None:
        @tenacity.retry(
            stop=tenacity.stop_after_attempt(3),
            wait=tenacity.wait_exponential(multiplier=1, min=1, max=9),
            reraise=True,
        )
        async def _attempt() -> None:
            await _bitable_request(
                self._client,
                lark.HttpMethod.POST,
                f"/open-apis/bitable/v1/apps/{self._settings.bitable_app_token}"
                f"/tables/{self._settings.bitable_table_id}/records",
                body={"fields": fields},
            )

        await _attempt()

    async def _update_record(self, record_id: str, fields: dict[str, object]) -> None:
        @tenacity.retry(
            stop=tenacity.stop_after_attempt(3),
            wait=tenacity.wait_exponential(multiplier=1, min=1, max=9),
            reraise=True,
        )
        async def _attempt() -> None:
            await _bitable_request(
                self._client,
                lark.HttpMethod.PUT,
                f"/open-apis/bitable/v1/apps/{self._settings.bitable_app_token}"
                f"/tables/{self._settings.bitable_table_id}/records/{record_id}",
                body={"fields": fields},
            )

        await _attempt()

    async def _find_records_by_normalized_url(
        self, normalized: str
    ) -> list[tuple[str, dict[str, object]]]:
        """Full-table scan for rows whose "链接" normalizes to the same key,
        returning (record_id, fields) so callers can carry values over.

        The stored URL string varies by share-tracking params, so the server
        can't filter by the normalized value directly; this fetches only
        record_id + a few fields (cheap payload) and compares client-side,
        the same fetch-all-then-filter approach `fetch_day` already uses for
        the same reason (unreliable server-side filtering).
        """
        uri = (
            f"/open-apis/bitable/v1/apps/{self._settings.bitable_app_token}"
            f"/tables/{self._settings.bitable_table_id}/records/search"
        )
        body = {
            "field_names": [_DISPLAY["url"], _DISPLAY["remark"], _DISPLAY["bibigpt_url"]],
            "automatic_fields": False,
        }
        matches: list[tuple[str, dict[str, object]]] = []
        page_token = ""
        while True:
            queries: list[tuple[str, str]] = [("page_size", "500")]
            if page_token:
                queries.append(("page_token", page_token))
            data = await _bitable_request(
                self._client, lark.HttpMethod.POST, uri, body=body, queries=queries
            )
            for item in data.get("items") or []:
                fields = item.get("fields") or {}
                url_value = fields.get(_DISPLAY["url"])
                url = str(url_value.get("link") or "") if isinstance(url_value, dict) else ""
                record_id = str(item.get("record_id") or "")
                if record_id and url and normalize_url(url) == normalized:
                    matches.append((record_id, fields))
            page_token = str(data.get("page_token") or "")
            if not data.get("has_more") or not page_token:
                break
        return matches

    async def _delete_record(self, record_id: str) -> None:
        try:
            await _bitable_request(
                self._client,
                lark.HttpMethod.DELETE,
                f"/open-apis/bitable/v1/apps/{self._settings.bitable_app_token}"
                f"/tables/{self._settings.bitable_table_id}/records/{record_id}",
            )
        except Exception as e:
            logger.warning(
                "failed to delete stale duplicate row: record_id=%s error=%s", record_id, e
            )

    def _encode_fields(self, entry: ArchiveEntry) -> dict[str, object]:
        url = entry.url
        title = entry.title or url
        duration = fmt_duration(entry.duration_seconds) if entry.duration_seconds else ""
        return {
            _DISPLAY["title"]: title,
            _DISPLAY["url"]: {"link": url, "text": url},
            _DISPLAY["platform"]: entry.platform,
            _DISPLAY["channel"]: entry.channel,
            _DISPLAY["duration"]: duration,
            # Bitable User fields take a list of {"id": open_id}; the display
            # name/avatar is resolved server-side, so no name lookup is needed here.
            _DISPLAY["sender"]: [{"id": entry.sender_open_id}],
            _DISPLAY["chat"]: entry.chat_name or entry.chat_id,
            # Bitable DateTime fields take a UTC epoch-millisecond int on write.
            _DISPLAY["recorded_at"]: int(entry.recorded_at_utc.timestamp() * 1000),
        }

    async def fetch_day(self, day: date) -> list[ArchivedRow]:
        """ "记录时间" is a bitable DateTime field (epoch ms). Its range-filter
        operators (isGreater/isGreaterEqual with an exact-day boundary) proved
        unreliable in practice — silently returning zero rows even for correct
        boundaries — so this fetches every row unfiltered and matches the day
        client-side against the decoded Beijing-time prefix instead.
        """
        prefix = day.isoformat()
        uri = (
            f"/open-apis/bitable/v1/apps/{self._settings.bitable_app_token}"
            f"/tables/{self._settings.bitable_table_id}/records/search"
        )
        body = {
            "field_names": [display for _, display, _ in _FIELDS],
            "automatic_fields": False,
        }
        rows: list[ArchivedRow] = []
        page_token = ""
        while True:
            queries: list[tuple[str, str]] = [("page_size", "500")]
            if page_token:
                queries.append(("page_token", page_token))
            data = await _bitable_request(
                self._client, lark.HttpMethod.POST, uri, body=body, queries=queries
            )
            for item in data.get("items") or []:
                row = _decode_row(item.get("fields") or {})
                if row.recorded_at.startswith(prefix):
                    rows.append(row)
            page_token = str(data.get("page_token") or "")
            if not data.get("has_more") or not page_token:
                break
        rows.sort(key=lambda r: r.recorded_at)
        return rows

    async def bootstrap(self, name: str = "飞书链接存档") -> tuple[str, str]:
        """One-time: create the base and table; returns (app_token, table_id).
        The caller fills these into config.yaml manually."""
        data = await _bitable_request(
            self._client,
            lark.HttpMethod.POST,
            "/open-apis/bitable/v1/apps",
            body={"name": name},
        )
        app_token = str((data.get("app") or {}).get("app_token") or "")
        if not app_token:
            raise ArchiveError(f"bitable app created but no app_token in response: {data}")

        existing = await _bitable_request(
            self._client,
            lark.HttpMethod.GET,
            f"/open-apis/bitable/v1/apps/{app_token}/tables",
        )
        default_table_ids = [str(t.get("table_id") or "") for t in existing.get("items") or []]

        created = await _bitable_request(
            self._client,
            lark.HttpMethod.POST,
            f"/open-apis/bitable/v1/apps/{app_token}/tables",
            body={
                "table": {
                    "name": "链接存档",
                    "default_view_name": "全部",
                    "fields": [
                        _bootstrap_field_spec(display, ftype) for _, display, ftype in _FIELDS
                    ],
                }
            },
        )
        table_id = str(created.get("table_id") or "")
        if not table_id:
            raise ArchiveError(f"table created but no table_id in response: {created}")

        for stale_id in default_table_ids:
            if stale_id and stale_id != table_id:
                await _bitable_request(
                    self._client,
                    lark.HttpMethod.DELETE,
                    f"/open-apis/bitable/v1/apps/{app_token}/tables/{stale_id}",
                )

        # Without this the app-owned base is invisible to humans. Failure is
        # non-fatal: sharing can be granted manually in the Feishu UI.
        try:
            await _bitable_request(
                self._client,
                lark.HttpMethod.PATCH,
                f"/open-apis/drive/v1/permissions/{app_token}/public",
                body={"link_share_entity": "tenant_editable"},
                queries=[("type", "bitable")],
            )
        except Exception as e:
            logger.warning(
                "failed to enable tenant sharing on base %s (grant manually): %s",
                app_token,
                e,
            )
        return app_token, table_id

    async def migrate(self) -> list[str]:
        """One-time: add any _FIELDS column missing from the existing table
        (via the API, not the UI, so no shadow fields). Returns the display
        names of the columns it created."""
        uri = (
            f"/open-apis/bitable/v1/apps/{self._settings.bitable_app_token}"
            f"/tables/{self._settings.bitable_table_id}/fields"
        )
        existing: set[str] = set()
        page_token = ""
        while True:
            queries: list[tuple[str, str]] = [("page_size", "100")]
            if page_token:
                queries.append(("page_token", page_token))
            data = await _bitable_request(self._client, lark.HttpMethod.GET, uri, queries=queries)
            for item in data.get("items") or []:
                name = str(item.get("field_name") or "")
                if name:
                    existing.add(name)
            page_token = str(data.get("page_token") or "")
            if not data.get("has_more") or not page_token:
                break

        created: list[str] = []
        for _, display, ftype in _FIELDS:
            if display in existing:
                continue
            await _bitable_request(
                self._client,
                lark.HttpMethod.POST,
                uri,
                body=_bootstrap_field_spec(display, ftype),
            )
            created.append(display)
            logger.info("bitable field created: %s", display)
        return created


def _bootstrap_field_spec(display: str, ftype: int) -> dict[str, object]:
    spec: dict[str, object] = {"field_name": display, "type": ftype}
    if ftype == _DATE_TIME:
        spec["property"] = {"date_formatter": "yyyy/MM/dd", "auto_fill": False}
    elif ftype == _USER:
        spec["property"] = {"multiple": True}
    return spec


def _carried_over_fields(
    stale: list[tuple[str, dict[str, object]]],
) -> dict[str, object]:
    """Extract the remark / BibiGPT-link values a replacement row must inherit
    from the stale duplicate row(s) it is about to displace."""
    carried: dict[str, object] = {}
    remarks: list[str] = []
    for _, fields in stale:
        remark = _flatten_value(fields.get(_DISPLAY["remark"]))
        if remark and remark not in remarks:
            remarks.append(remark)
        if _DISPLAY["bibigpt_url"] not in carried:
            link = _decode_url_value(fields.get(_DISPLAY["bibigpt_url"]))
            if link:
                carried[_DISPLAY["bibigpt_url"]] = {"link": link, "text": link}
    if remarks:
        carried[_DISPLAY["remark"]] = "\n".join(remarks)
    return carried


def _decode_url_value(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("link") or value.get("text") or "")
    return _flatten_value(value)


def _decode_row(fields: dict[str, object]) -> ArchivedRow:
    def text(logical: str) -> str:
        return _flatten_value(fields.get(_DISPLAY[logical]))

    return ArchivedRow(
        title=text("title"),
        url=_decode_url_value(fields.get(_DISPLAY["url"])),
        platform=text("platform"),
        channel=text("channel"),
        duration=text("duration"),
        sender=_decode_sender(fields.get(_DISPLAY["sender"])),
        chat=text("chat"),
        recorded_at=_decode_recorded_at(fields.get(_DISPLAY["recorded_at"])),
        remark=text("remark"),
        bibigpt_url=_decode_url_value(fields.get(_DISPLAY["bibigpt_url"])),
    )


def _decode_sender(value: object) -> str:
    """The field is a bitable People field: a list of user objects with a
    server-resolved display name. Older rows written back when it was still
    a text field may still hold a plain string, so that shape is handled too."""
    if isinstance(value, list) and value and isinstance(value[0], dict) and "id" in value[0]:
        first = value[0]
        return str(first.get("name") or first.get("en_name") or first.get("id") or "")
    return _flatten_value(value)


def _decode_recorded_at(value: object) -> str:
    """The field is a bitable DateTime (epoch ms as int/float); older rows
    written back when it was still a text field may still hold a plain
    "YYYY-MM-DD HH:MM" string, so both shapes are handled defensively."""
    if isinstance(value, int | float):
        return format_beijing(datetime.fromtimestamp(value / 1000, tz=UTC), _RECORDED_AT_FMT)
    return _flatten_value(value)


def _flatten_value(value: object) -> str:
    """Bitable search returns text values either as plain strings or as lists
    of segment dicts ({"text": ..., "type": ...}); decode both defensively."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for seg in value:
            if isinstance(seg, dict):
                parts.append(str(seg.get("text") or seg.get("link") or ""))
            else:
                parts.append(str(seg))
        return "".join(parts)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("link") or "")
    return str(value)
