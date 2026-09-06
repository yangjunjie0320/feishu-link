"""Bounded SDK calls without its synchronous token-cache-miss request."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from dataclasses import dataclass, field
from typing import Any
from weakref import WeakKeyDictionary

import httpx
import lark_oapi as lark


@dataclass
class _TenantToken:
    value: str = ""
    expires_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_TOKENS: WeakKeyDictionary[lark.Client, _TenantToken] = WeakKeyDictionary()


async def _tenant_token(client: lark.Client, timeout: float) -> str:
    state = _TOKENS.setdefault(client, _TenantToken())
    async with state.lock:
        if state.value and time.monotonic() < state.expires_at:
            return state.value
        config = getattr(client, "config", None) or getattr(client, "_config", None)
        if config is None or not config.app_id or not config.app_secret:
            raise ValueError("Feishu app credentials are not configured")
        if (
            config.app_type != lark.AppType.SELF
            or getattr(config, "client_assertion_provider", None) is not None
        ):
            raise ValueError("Async Feishu token acquisition requires a self-built app secret")
        url = config.domain.rstrip("/") + "/open-apis/auth/v3/tenant_access_token/internal"
        async with httpx.AsyncClient(timeout=timeout) as http:
            response = await http.post(
                url, json={"app_id": config.app_id, "app_secret": config.app_secret}
            )
            response.raise_for_status()
            data = response.json()
        token = data.get("tenant_access_token")
        if data.get("code") != 0 or not isinstance(token, str) or not token:
            # The response body can contain credentials; only report the code.
            raise ValueError(f"Feishu token request failed: code={data.get('code')}")
        lifetime = max(0.0, float(data.get("expire") or 0))
        state.value = token
        state.expires_at = time.monotonic() + max(0.0, lifetime - min(60.0, lifetime / 2))
        return token


async def call_feishu_async(
    client: lark.Client,
    resource_name: str | None,
    method: str,
    request: Any,
    *,
    timeout: float,
) -> Any:
    """Call an im.v1 resource (or BaseClient) within one budget, including auth.

    SDK async methods still call synchronous ``verify`` before their first
    await. A copied resource/config plus an explicit, asynchronously acquired
    token avoids that path without changing other users of the shared client.
    """
    async with asyncio.timeout(timeout):
        resource = getattr(client.im.v1, resource_name) if resource_name else client
        if not isinstance(client, lark.Client):
            # Duck-typed injected clients already own their async transport.
            return await getattr(resource, method)(request)
        token = await _tenant_token(client, timeout)
        resource = copy.copy(resource)
        config_attr = "config" if resource_name else "_config"
        config = copy.copy(getattr(resource, config_attr))
        config.enable_set_token = True
        config.timeout = timeout
        setattr(resource, config_attr, config)
        option = lark.RequestOption.builder().tenant_access_token(token).build()
        response = await getattr(resource, method)(request, option)
        if resource_name is None:
            # BaseClient.arequest checks a case-sensitive Content-Type key,
            # while its async transport returns lower-case HTTPX headers.
            # Recover the API result that older SDKs replace with HTTP success.
            try:
                payload = json.loads(response.raw.content)
            except (AttributeError, TypeError, ValueError):
                payload = None
            if isinstance(payload, dict) and isinstance(payload.get("code"), int):
                response.code = payload["code"]
                response.msg = payload.get("msg")
        if getattr(getattr(response, "raw", None), "status_code", None) == 401:
            _TOKENS.pop(client, None)
        return response
