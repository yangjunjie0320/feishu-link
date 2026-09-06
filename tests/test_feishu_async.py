import httpx
import lark_oapi as lark
import pytest
import respx

from src.feishu_async import call_feishu_async


@respx.mock
@pytest.mark.parametrize("code", [0, 1254066, 99991672])
async def test_generic_sdk_request_preserves_json_result_with_lowercase_headers(code: int) -> None:
    respx.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal").mock(
        return_value=httpx.Response(200, json={
            "code": 0, "tenant_access_token": "test_generic_token", "expire": 3600,
        })
    )
    respx.post("https://open.feishu.cn/open-apis/bitable/test").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "application/json; charset=utf-8"},
            json={"code": code, "msg": "API result", "data": {}},
        )
    )
    client = lark.Client.builder().app_id("test_app").app_secret("test_secret").build()
    request = (
        lark.BaseRequest.builder()
        .http_method(lark.HttpMethod.POST)
        .uri("/open-apis/bitable/test")
        .token_types({lark.AccessTokenType.TENANT})
        .body({"fields": {}})
        .build()
    )

    response = await call_feishu_async(client, None, "arequest", request, timeout=2)

    assert response.raw.status_code == 200
    assert response.code == code
    assert response.msg == "API result"
    assert response.success() is (code == 0)
    assert client._config.enable_set_token is False
