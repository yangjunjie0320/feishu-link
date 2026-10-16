from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx
from dateutil import tz

from .card import build_notice_card
from .config import Settings
from .http_errors import describe_request_error
from .sender import CardSender

logger = logging.getLogger(__name__)

_EXT_CTL_RE = re.compile(r"-ext-ctl\s+(\S+)")
_DELAY_TEST_URL = "https://www.gstatic.com/generate_204"
_DELAY_TEST_TIMEOUT_MS = 5000
_SWITCH_SETTLE_SECONDS = 2.0
_MAX_GROUP_DEPTH = 5
_FIELD_GAP = "\u3000"
# Short names for the probe table; unknown hosts are shown as-is.
_TARGET_LABELS = {
    "api.anthropic.com": "Anthropic",
    "www.youtube.com": "YouTube",
    "youtube.com": "YouTube",
    "www.instagram.com": "Instagram",
    "instagram.com": "Instagram",
    "x.com": "X",
    "twitter.com": "X",
}


class ControllerUnavailableError(RuntimeError):
    """The mihomo external controller could not be found or did not answer."""


@dataclass(frozen=True)
class ProbeResult:
    url: str
    status: int | None
    error: str
    elapsed_ms: int

    def reachable(self) -> bool:
        return self.status is not None

    def passes_required(self) -> bool:
        # api.anthropic.com answers 403 to regions it does not serve; any other
        # HTTP answer (401 without a key) proves the egress works for it.
        return self.status is not None and self.status != 403

    def describe(self) -> str:
        host = httpx.URL(self.url).host
        if self.status is None:
            return f"{host} 不通（{self.error}）"
        return f"{host} HTTP {self.status} {self.elapsed_ms}ms"

    def label(self) -> str:
        host = httpx.URL(self.url).host
        return _TARGET_LABELS.get(host, host)

    def short_error(self) -> str:
        error = self.error.lower()
        if "timeout" in error or "timed out" in error:
            return "超时"
        if "proxy" in error:
            return "代理错误"
        if "connect" in error:
            return "连接失败"
        return "不通"

    def cell(self, required: bool) -> str:
        if self.status is None:
            return f"失败 {self.short_error()}"
        if required and not self.passes_required():
            return f"失败 {self.status}"
        return f"正常 {self.elapsed_ms}ms"


@dataclass(frozen=True)
class ProbeRound:
    required: list[ProbeResult]
    optional: list[ProbeResult]

    def healthy(self) -> bool:
        if not all(result.passes_required() for result in self.required):
            return False
        if not self.optional:
            return True
        reachable = sum(1 for result in self.optional if result.reachable())
        return reachable * 2 >= len(self.optional)

    def summary(self) -> str:
        return "；".join(result.describe() for result in [*self.required, *self.optional])

    def cells(self) -> list[tuple[str, str]]:
        return [(r.label(), r.cell(True)) for r in self.required] + [
            (r.label(), r.cell(False)) for r in self.optional
        ]

    def brief(self) -> str:
        return "，".join(f"{label} {cell}" for label, cell in self.cells())

    def failure_reason(self) -> str:
        for result in self.required:
            if result.status is None:
                return f"{result.label()} 不通（{result.short_error()}）"
            if not result.passes_required():
                return f"{result.label()} 返回 {result.status}（该地区被拒）"
        down = [result.label() for result in self.optional if not result.reachable()]
        if down:
            return f"{'、'.join(down)} 不通"
        return "检测未通过"


@dataclass(frozen=True)
class Notice:
    title: str
    body: str
    template: str
    details: str = ""


def _fields(*pairs: tuple[str, str]) -> str:
    return "\n".join(f"**{label}**{_FIELD_GAP}{value}" for label, value in pairs)


def _probe_table(columns: list[tuple[str, ProbeRound]]) -> str:
    header = ["目标", *(name for name, _ in columns)]
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
    ]
    rows = [probes.cells() for _, probes in columns]
    for index, (label, _) in enumerate(rows[0]):
        lines.append("| " + " | ".join([label, *(row[index][1] for row in rows)]) + " |")
    return "\n".join(lines)


def _compose(lead: str, fields: str, table: str = "") -> str:
    return "\n\n".join(part for part in (f"**{lead}**", fields, table) if part)


Probe = Callable[[str, bool], Awaitable[ProbeResult]]


async def probe_url(settings: Settings, url: str, via_proxy: bool) -> ProbeResult:
    """One GET with a fresh client: a pooled CONNECT tunnel would keep going
    through the node that was selected when it opened."""
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(
            proxy=settings.net_monitor_proxy_url if via_proxy else None,
            trust_env=False,
            timeout=settings.net_monitor_probe_timeout,
            follow_redirects=False,
        ) as client:
            response = await client.get(url)
        return ProbeResult(url, response.status_code, "", _elapsed_ms(started))
    except httpx.HTTPError as e:
        return ProbeResult(url, None, describe_request_error(e), _elapsed_ms(started))


class MihomoController:
    """Minimal client for the mihomo (Clash Meta) external controller API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base_url = settings.net_monitor_controller_url.rstrip("/")

    async def _discover(self) -> str:
        process = await asyncio.create_subprocess_exec(
            "ps", "-axo", "command",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        output, _ = await process.communicate()
        match = _EXT_CTL_RE.search(output.decode(errors="replace"))
        if not match:
            raise ControllerUnavailableError("no running core with -ext-ctl found")
        return f"http://{match.group(1)}"

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if not self._base_url:
            self._base_url = await self._discover()
        headers = {}
        if self._settings.net_monitor_controller_secret:
            headers["Authorization"] = f"Bearer {self._settings.net_monitor_controller_secret}"
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, trust_env=False, timeout=30.0, headers=headers,
            ) as client:
                response = await client.request(method, path, **kwargs)
            response.raise_for_status()
            return response
        except httpx.HTTPError as e:
            if not self._settings.net_monitor_controller_url:
                # The port may have moved after a TgfishVPN restart.
                self._base_url = ""
            raise ControllerUnavailableError(
                f"controller {method} {path} failed: {describe_request_error(e)}"
            ) from e

    async def group(self, name: str) -> dict[str, Any]:
        response = await self._request("GET", f"/proxies/{quote(name, safe='')}")
        return response.json()

    async def resolve_node(self, group: str) -> str:
        """Follow `now` through nested groups down to the node in use."""
        name = group
        for _ in range(_MAX_GROUP_DEPTH):
            info = await self.group(name)
            if not info.get("all"):
                return name
            name = str(info.get("now") or "")
            if not name:
                break
        return name

    async def mode(self) -> str:
        response = await self._request("GET", "/configs")
        return str(response.json().get("mode") or "")

    async def set_mode(self, mode: str) -> None:
        await self._request("PATCH", "/configs", json={"mode": mode})

    async def select(self, group: str, node: str) -> None:
        await self._request("PUT", f"/proxies/{quote(group, safe='')}", json={"name": node})

    async def group_delays(self, group: str) -> dict[str, int]:
        response = await self._request(
            "GET",
            f"/group/{quote(group, safe='')}/delay",
            params={"url": _DELAY_TEST_URL, "timeout": _DELAY_TEST_TIMEOUT_MS},
        )
        data = response.json()
        return {str(name): int(ms) for name, ms in data.items() if isinstance(ms, int | float)}


class NetMonitor:
    def __init__(
        self,
        settings: Settings,
        sender: CardSender,
        *,
        controller: MihomoController | None = None,
        probe: Probe | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._sender = sender
        self._controller = controller or MihomoController(settings)
        self._probe = probe or (lambda url, via_proxy: probe_url(settings, url, via_proxy))
        self._sleep = sleep
        self._zone = tz.gettz(settings.report_timezone)
        self._feishu_down_since: datetime | None = None
        self._pending: list[Notice] = []
        self._outage_alerted = False

    async def run(self) -> None:
        logger.info(
            "net monitor started: interval=%ss allowed=%s",
            self._settings.net_monitor_interval_seconds,
            self._settings.net_monitor_allowed_node_keywords,
        )
        while True:
            try:
                await self.check_once()
            except Exception:
                logger.exception("net monitor check failed")
            await self._sleep(self._settings.net_monitor_interval_seconds)

    async def check_once(self) -> None:
        await self._check_feishu()

        try:
            node = await self._controller.resolve_node(self._settings.net_monitor_selector_group)
        except ControllerUnavailableError as e:
            logger.warning("net monitor: %s", e)
            probes = await self._probe_round()
            if not probes.healthy():
                await self._alert_outage(
                    Notice(
                        "网络监控：没有可用节点",
                        _compose(
                            "代理内核管理接口不可用，且代理检测失败",
                            _fields(("原因", probes.failure_reason()), ("时间", self._now())),
                            _probe_table([("检测结果", probes)]),
                        ),
                        "red",
                        f"管理接口：{e}\n\n检测：{probes.summary()}",
                    ),
                    f"controller unavailable ({e}); {probes.summary()}",
                )
            return

        if not await self._ensure_mode():
            return

        probes = await self._probe_round()
        if not probes.healthy():
            logger.warning(
                "net monitor probe failed, rechecking in %ss: node=%s %s",
                self._settings.net_monitor_confirm_delay_seconds,
                node,
                probes.summary(),
            )
            await self._sleep(self._settings.net_monitor_confirm_delay_seconds)
            node = await self._controller.resolve_node(self._settings.net_monitor_selector_group)
            probes = await self._probe_round()

        if probes.healthy() and self._allowed(node):
            logger.info("net monitor ok: node=%s %s", node, probes.summary())
            await self._clear_outage(node, probes)
            return

        if probes.healthy():
            keywords = "、".join(self._settings.net_monitor_allowed_node_keywords)
            reason = f"当前节点不在白名单（{keywords}）"
        else:
            reason = probes.failure_reason()
        await self._switch(node, reason, probes)

    async def _ensure_mode(self) -> bool:
        """Patch the core back to the expected mode; False if it stays wrong.

        In "direct" mode every request bypasses the nodes, so probing or
        switching nodes would only misreport an outage of all of them.
        """
        expected = self._settings.net_monitor_expected_mode.strip().lower()
        if not expected:
            return True
        try:
            current = (await self._controller.mode()).lower()
        except ControllerUnavailableError as e:
            logger.warning("net monitor: cannot read core mode: %s", e)
            return True
        if current == expected:
            return True

        error = ""
        try:
            await self._controller.set_mode(expected)
            restored = (await self._controller.mode()).lower() == expected
        except ControllerUnavailableError as e:
            restored, error = False, str(e)

        impact = "所有流量绕过节点直连，X / YouTube / Instagram / Anthropic 都会不通"
        if restored:
            logger.warning("net monitor: core mode was %s, restored to %s", current, expected)
            await self._notify(
                Notice(
                    "网络监控：代理模式已纠正",
                    _compose(
                        f"代理内核处于「{current}」模式，已切回「{expected}」",
                        _fields(("影响", impact), ("时间", self._now())),
                    ),
                    "orange",
                )
            )
            await self._sleep(_SWITCH_SETTLE_SECONDS)
            return True

        logger.error(
            "net monitor: core stuck in %s mode, restore to %s failed: %s",
            current, expected, error or "mode unchanged",
        )
        if not self._outage_alerted:
            self._outage_alerted = True
            title = "代理处于直连模式" if current == "direct" else "代理模式异常"
            await self._notify(
                Notice(
                    f"网络监控：{title}",
                    _compose(
                        f"代理内核处于「{current}」模式，自动切回「{expected}」失败"
                        "，节点本身未必有问题",
                        _fields(
                            ("影响", impact),
                            ("处理", "在 TgfishVPN 客户端里切到「规则」模式"),
                            ("时间", self._now()),
                        ),
                    ),
                    "red",
                    error,
                )
            )
        return False

    async def _switch(self, current: str, reason: str, before: ProbeRound) -> None:
        group = self._settings.net_monitor_selector_group
        delays = await self._controller.group_delays(self._settings.net_monitor_candidate_group)
        candidates = sorted(
            (ms, name)
            for name, ms in delays.items()
            if name != current and self._allowed(name)
        )[: self._settings.net_monitor_max_switch_attempts]
        if not candidates:
            await self._alert_outage(
                Notice(
                    "网络监控：没有可用节点",
                    _compose(
                        f"白名单内没有测速可达的节点，仍在用「{current}」",
                        _fields(("原因", reason), ("时间", self._now())),
                        _probe_table([("当前节点", before)]),
                    ),
                    "red",
                    f"{current}：{before.summary()}",
                ),
                f"{reason}; no allowed node passed the delay test",
            )
            return

        original = str((await self._controller.group(group)).get("now") or "")
        attempts: list[tuple[str, ProbeRound]] = []
        for ms, name in candidates:
            await self._controller.select(group, name)
            await self._sleep(_SWITCH_SETTLE_SECONDS)
            probes = await self._probe_round()
            if probes.healthy():
                logger.warning(
                    "net monitor switched node: %s -> %s reason=%s", current, name, reason
                )
                fields = [("原因", reason), ("新节点测速", f"{ms}ms")]
                if attempts:
                    fields.append(("已跳过", "、".join(skipped for skipped, _ in attempts)))
                fields.append(("时间", self._now()))
                await self._notify(
                    Notice(
                        "网络监控：已切换节点",
                        _compose(
                            f"出口已从「{current}」切换到「{name}」",
                            _fields(*fields),
                            _probe_table([("切换前", before), ("切换后", probes)]),
                        ),
                        "orange",
                        _attempt_details([(current, before), *attempts, (name, probes)]),
                    )
                )
                self._outage_alerted = False
                return
            attempts.append((name, probes))

        restored = original or current
        if original:
            await self._controller.select(group, original)
        await self._alert_outage(
            Notice(
                "网络监控：没有可用节点",
                _compose(
                    f"所有候选节点都不通，已恢复原选择「{restored}」",
                    _fields(
                        ("原因", reason),
                        ("已尝试", "、".join(name for name, _ in attempts)),
                        ("时间", self._now()),
                    ),
                    _probe_table([(current, before)]),
                ),
                "red",
                _attempt_details([(current, before), *attempts]),
            ),
            f"{reason}; tried: "
            + "; ".join(f"{name} ({probes.summary()})" for name, probes in attempts),
        )

    async def _alert_outage(self, notice: Notice, detail: str) -> None:
        logger.error("net monitor: no usable proxy node: %s", detail)
        if self._outage_alerted:
            return
        self._outage_alerted = True
        await self._notify(notice)

    async def _clear_outage(self, node: str, probes: ProbeRound) -> None:
        if not self._outage_alerted:
            return
        self._outage_alerted = False
        await self._notify(
            Notice(
                "网络监控：代理已恢复",
                _compose(
                    f"代理已恢复，当前节点「{node}」",
                    _fields(("时间", self._now())),
                    _probe_table([("当前", probes)]),
                ),
                "green",
            )
        )

    async def _check_feishu(self) -> None:
        result = await self._probe(self._settings.net_monitor_feishu_url, False)
        if not result.reachable():
            if self._feishu_down_since is None:
                self._feishu_down_since = self._now_dt()
            logger.warning("net monitor: feishu unreachable: %s", result.describe())
            return
        if self._feishu_down_since is None:
            return
        since = self._feishu_down_since
        self._feishu_down_since = None
        pending, self._pending = self._pending, []
        fields = [("不可达时段", f"{self._fmt(since)} 至 {self._now()}")]
        if pending:
            events = "、".join(notice.title.removeprefix("网络监控：") for notice in pending)
            fields.append(("期间事件", events))
            lead = "飞书曾不可达，期间消息随后补发"
        else:
            lead = "飞书曾不可达，现已恢复"
        notice = Notice("网络监控：飞书曾不可达", _compose(lead, _fields(*fields)), "grey")
        if not await self._send(notice):
            self._pending = pending
            return
        for queued in pending:
            await self._notify(queued)

    async def _notify(self, notice: Notice) -> None:
        if self._feishu_down_since is not None:
            self._pending.append(notice)
            logger.warning("net monitor: feishu down, queued notice: %s", notice.title)
            return
        if not await self._send(notice):
            self._pending.append(notice)
            self._feishu_down_since = self._now_dt()

    async def _send(self, notice: Notice) -> bool:
        chat_id = self._settings.effective_net_monitor_chat_id()
        if not chat_id:
            logger.warning("net monitor notice dropped: no chat configured: %s", notice.title)
            return True
        card = build_notice_card(
            notice.title, notice.body, template=notice.template, details=notice.details
        )
        return await self._sender.send_to_chat(card, chat_id)

    async def _probe_round(self) -> ProbeRound:
        required = self._settings.net_monitor_required_urls
        optional = self._settings.net_monitor_optional_urls
        results = await asyncio.gather(*(self._probe(url, True) for url in [*required, *optional]))
        return ProbeRound(list(results[: len(required)]), list(results[len(required) :]))

    def _allowed(self, node: str) -> bool:
        return any(keyword in node for keyword in self._settings.net_monitor_allowed_node_keywords)

    def _now_dt(self) -> datetime:
        return datetime.now(tz=self._zone)

    def _now(self) -> str:
        return self._fmt(self._now_dt())

    @staticmethod
    def _fmt(moment: datetime) -> str:
        return moment.strftime("%m-%d %H:%M")


def _attempt_details(rounds: list[tuple[str, ProbeRound]]) -> str:
    return "\n".join(f"- {name}：{probes.summary()}" for name, probes in rounds)


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
