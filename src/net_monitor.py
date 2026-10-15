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

from .card import build_markdown_card
from .config import Settings
from .http_errors import describe_request_error
from .sender import CardSender

logger = logging.getLogger(__name__)

_EXT_CTL_RE = re.compile(r"-ext-ctl\s+(\S+)")
_DELAY_TEST_URL = "https://www.gstatic.com/generate_204"
_DELAY_TEST_TIMEOUT_MS = 5000
_SWITCH_SETTLE_SECONDS = 2.0
_MAX_GROUP_DEPTH = 5


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
        self._pending: list[tuple[str, str]] = []
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
                    f"代理内核管理接口不可用（{e}），且代理检测失败：{probes.summary()}"
                )
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
            await self._clear_outage(node)
            return

        if probes.healthy():
            reason = f"当前节点 {node} 不在白名单"
        else:
            reason = f"节点 {node} 检测失败：{probes.summary()}"
        await self._switch(node, reason)

    async def _switch(self, current: str, reason: str) -> None:
        group = self._settings.net_monitor_selector_group
        delays = await self._controller.group_delays(self._settings.net_monitor_candidate_group)
        candidates = sorted(
            (ms, name)
            for name, ms in delays.items()
            if name != current and self._allowed(name)
        )[: self._settings.net_monitor_max_switch_attempts]
        if not candidates:
            await self._alert_outage(f"{reason}；白名单内没有测速可达的节点")
            return

        original = str((await self._controller.group(group)).get("now") or "")
        attempts: list[str] = []
        for ms, name in candidates:
            await self._controller.select(group, name)
            await self._sleep(_SWITCH_SETTLE_SECONDS)
            probes = await self._probe_round()
            if probes.healthy():
                logger.warning(
                    "net monitor switched node: %s -> %s reason=%s", current, name, reason
                )
                await self._notify(
                    "网络监控：已切换节点",
                    f"- 原节点：{current}\n- 新节点：{name}（测速 {ms}ms）\n"
                    f"- 原因：{reason}\n- 检测：{probes.summary()}\n- 时间：{self._now()}",
                )
                self._outage_alerted = False
                return
            attempts.append(f"{name}（{probes.summary()}）")

        if original:
            await self._controller.select(group, original)
        await self._alert_outage(f"{reason}；已尝试：{'；'.join(attempts)}")

    async def _alert_outage(self, detail: str) -> None:
        logger.error("net monitor: no usable proxy node: %s", detail)
        if self._outage_alerted:
            return
        self._outage_alerted = True
        await self._notify("网络监控：没有可用节点", f"- {detail}\n- 时间：{self._now()}")

    async def _clear_outage(self, node: str) -> None:
        if not self._outage_alerted:
            return
        self._outage_alerted = False
        await self._notify("网络监控：代理已恢复", f"- 当前节点：{node}\n- 时间：{self._now()}")

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
        lines = [f"- 不可达时段：{self._fmt(since)} 至 {self._now()}"]
        for title, _ in self._pending:
            lines.append(f"- 期间事件：{title}")
        pending, self._pending = self._pending, []
        if not await self._send("网络监控：飞书曾不可达", "\n".join(lines)):
            self._pending = pending
            return
        for title, markdown in pending:
            await self._notify(title, markdown)

    async def _notify(self, title: str, markdown: str) -> None:
        if self._feishu_down_since is not None:
            self._pending.append((title, markdown))
            logger.warning("net monitor: feishu down, queued notice: %s", title)
            return
        if not await self._send(title, markdown):
            self._pending.append((title, markdown))
            self._feishu_down_since = self._now_dt()

    async def _send(self, title: str, markdown: str) -> bool:
        chat_id = self._settings.effective_net_monitor_chat_id()
        if not chat_id:
            logger.warning("net monitor notice dropped: no chat configured: %s", title)
            return True
        return await self._sender.send_to_chat(build_markdown_card(title, markdown), chat_id)

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


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
