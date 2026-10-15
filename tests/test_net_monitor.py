from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from src.config import Settings
from src.net_monitor import (
    ControllerUnavailableError,
    MihomoController,
    NetMonitor,
    ProbeResult,
    ProbeRound,
)

ANTHROPIC = "https://api.anthropic.com/v1/models"
OPTIONAL = ["https://www.youtube.com/generate_204", "https://www.instagram.com/", "https://x.com/"]
FEISHU = "https://open.feishu.cn"


class FakeController:
    def __init__(self, now: str, delays: dict[str, int]) -> None:
        self.now = now
        self.delays = delays
        self.selected: list[str] = []
        self.unavailable = False

    async def resolve_node(self, group: str) -> str:
        if self.unavailable:
            raise ControllerUnavailableError("controller down")
        return self.now

    async def group(self, name: str) -> dict[str, Any]:
        return {"now": self.now, "all": list(self.delays)}

    async def select(self, group: str, node: str) -> None:
        self.selected.append(node)
        self.now = node

    async def group_delays(self, group: str) -> dict[str, int]:
        return dict(self.delays)


class Network:
    """Which nodes carry traffic, and whether Feishu answers directly."""

    def __init__(self, controller: FakeController, good_nodes: set[str]) -> None:
        self.controller = controller
        self.good_nodes = good_nodes
        self.feishu_up = True
        self.calls = 0

    async def probe(self, url: str, via_proxy: bool) -> ProbeResult:
        self.calls += 1
        if not via_proxy:
            return ProbeResult(url, 200 if self.feishu_up else None, "ConnectTimeout", 5)
        if self.controller.now in self.good_nodes:
            return ProbeResult(url, 401 if url == ANTHROPIC else 200, "", 100)
        return ProbeResult(url, None, "ConnectTimeout", 10000)


def settings() -> Settings:
    return Settings(
        net_monitor_enabled=True,
        net_monitor_chat_id="oc_monitor",
        net_monitor_required_urls=[ANTHROPIC],
        net_monitor_optional_urls=OPTIONAL,
        net_monitor_feishu_url=FEISHU,
    )


def build(
    now: str, delays: dict[str, int], good: set[str],
) -> tuple[NetMonitor, Network, AsyncMock]:
    controller = FakeController(now, delays)
    network = Network(controller, good)
    sender = AsyncMock()
    sender.send_to_chat = AsyncMock(return_value=True)
    monitor = NetMonitor(
        settings(), sender, controller=controller, probe=network.probe, sleep=AsyncMock(),
    )
    return monitor, network, sender


def sent_titles(sender: AsyncMock) -> list[str]:
    titles = []
    for call in sender.send_to_chat.await_args_list:
        card = json.loads(call.args[0])
        titles.append(card["header"]["title"]["content"])
    return titles


DELAYS = {"台湾 01": 107, "新加坡 01": 117, "香港 02": 90, "泰国02 家宽": 80, "日本 01": 150}


async def test_healthy_allowed_node_is_left_alone() -> None:
    monitor, network, sender = build("台湾 01", DELAYS, {"台湾 01"})

    await monitor.check_once()

    assert network.controller.selected == []
    sender.send_to_chat.assert_not_awaited()


async def test_healthy_node_outside_allowlist_moves_to_fastest_allowed() -> None:
    monitor, network, sender = build("泰国02 家宽", DELAYS, {"泰国02 家宽", "台湾 01"})

    await monitor.check_once()

    # 香港 is faster but not allowlisted.
    assert network.controller.selected == ["台湾 01"]
    assert sent_titles(sender) == ["网络监控：已切换节点"]
    assert sender.send_to_chat.await_args.args[1] == "oc_monitor"


async def test_failed_node_is_rechecked_then_switched_past_bad_candidates() -> None:
    monitor, network, sender = build("台湾 01", DELAYS, {"日本 01"})

    await monitor.check_once()

    monitor._sleep.assert_any_await(60.0)
    assert network.controller.selected == ["新加坡 01", "日本 01"]
    assert sent_titles(sender) == ["网络监控：已切换节点"]


async def test_blip_that_recovers_on_recheck_does_not_switch() -> None:
    monitor, network, sender = build("台湾 01", DELAYS, set())

    async def recover(_: float) -> None:
        network.good_nodes.add("台湾 01")

    monitor._sleep = AsyncMock(side_effect=recover)

    await monitor.check_once()

    assert network.controller.selected == []
    sender.send_to_chat.assert_not_awaited()


async def test_all_candidates_failing_restores_selection_and_alerts_once() -> None:
    monitor, network, sender = build("台湾 01", DELAYS, set())

    await monitor.check_once()
    await monitor.check_once()

    assert network.controller.selected[-1] == "台湾 01"
    assert sent_titles(sender) == ["网络监控：没有可用节点"]

    network.good_nodes.add("台湾 01")
    await monitor.check_once()

    assert sent_titles(sender) == ["网络监控：没有可用节点", "网络监控：代理已恢复"]


async def test_no_allowed_node_in_delay_test_alerts_directly() -> None:
    monitor, network, sender = build("台湾 01", {"香港 02": 90}, set())

    await monitor.check_once()

    assert network.controller.selected == []
    assert sent_titles(sender) == ["网络监控：没有可用节点"]


async def test_feishu_outage_is_logged_and_reported_on_first_reconnect() -> None:
    monitor, network, sender = build("泰国02 家宽", DELAYS, {"泰国02 家宽", "台湾 01"})
    network.feishu_up = False

    await monitor.check_once()

    assert network.controller.selected == ["台湾 01"]
    sender.send_to_chat.assert_not_awaited()

    network.feishu_up = True
    await monitor.check_once()

    assert sent_titles(sender) == ["网络监控：飞书曾不可达", "网络监控：已切换节点"]


async def test_failed_send_is_kept_for_the_next_reconnect() -> None:
    monitor, _, sender = build("泰国02 家宽", DELAYS, {"泰国02 家宽", "台湾 01"})
    sender.send_to_chat = AsyncMock(side_effect=[False, True, True])

    await monitor.check_once()
    await monitor.check_once()

    assert sent_titles(sender) == [
        "网络监控：已切换节点",
        "网络监控：飞书曾不可达",
        "网络监控：已切换节点",
    ]


async def test_controller_down_and_probes_failing_alerts() -> None:
    monitor, network, sender = build("台湾 01", DELAYS, set())
    network.controller.unavailable = True

    await monitor.check_once()

    assert sent_titles(sender) == ["网络监控：没有可用节点"]


def test_probe_round_rules() -> None:
    ok = ProbeResult(ANTHROPIC, 401, "", 1)
    blocked = ProbeResult(ANTHROPIC, 403, "", 1)
    up = ProbeResult(OPTIONAL[0], 204, "", 1)
    down = ProbeResult(OPTIONAL[1], None, "ConnectTimeout", 1)

    assert ProbeRound([ok], [up, up, down]).healthy()
    assert not ProbeRound([blocked], [up, up, up]).healthy()
    assert not ProbeRound([ok], [up, down, down]).healthy()


@respx.mock
async def test_mihomo_controller_encodes_group_names_and_follows_nested_groups() -> None:
    controller = MihomoController(Settings(net_monitor_controller_url="http://127.0.0.1:9"))
    base = "http://127.0.0.1:9"
    respx.get(f"{base}/proxies/TgfishAPP").mock(
        return_value=httpx.Response(200, json={"type": "Selector", "now": "自动选择", "all": ["a"]})
    )
    respx.get(f"{base}/proxies/%E8%87%AA%E5%8A%A8%E9%80%89%E6%8B%A9").mock(
        return_value=httpx.Response(200, json={"type": "URLTest", "now": "台湾 01", "all": ["a"]})
    )
    respx.get(f"{base}/proxies/%E5%8F%B0%E6%B9%BE%2001").mock(
        return_value=httpx.Response(200, json={"type": "Vless", "name": "台湾 01"})
    )
    put = respx.put(f"{base}/proxies/TgfishAPP").mock(return_value=httpx.Response(204))
    respx.get(f"{base}/group/%E8%87%AA%E5%8A%A8%E9%80%89%E6%8B%A9/delay").mock(
        return_value=httpx.Response(200, json={"台湾 01": 107, "bad": "x"})
    )

    assert await controller.resolve_node("TgfishAPP") == "台湾 01"
    await controller.select("TgfishAPP", "日本 01")
    assert json.loads(put.calls.last.request.content) == {"name": "日本 01"}
    assert await controller.group_delays("自动选择") == {"台湾 01": 107}


@respx.mock
async def test_mihomo_controller_error_is_wrapped() -> None:
    controller = MihomoController(Settings(net_monitor_controller_url="http://127.0.0.1:9"))
    respx.get("http://127.0.0.1:9/proxies/TgfishAPP").mock(return_value=httpx.Response(500))

    with pytest.raises(ControllerUnavailableError):
        await controller.resolve_node("TgfishAPP")


async def test_mihomo_controller_discovers_ext_ctl(monkeypatch: pytest.MonkeyPatch) -> None:
    class Process:
        async def communicate(self) -> tuple[bytes, bytes]:
            return (
                b"/usr/bin/foo\n/Users/x/.config/com.vortex.helper/com.vortex.helper -d /a "
                b"-f /a/config.yaml -ext-ctl 127.0.0.1:39798\n",
                b"",
            )

    monkeypatch.setattr(
        "src.net_monitor.asyncio.create_subprocess_exec", AsyncMock(return_value=Process())
    )
    controller = MihomoController(Settings())

    assert await controller._discover() == "http://127.0.0.1:39798"
