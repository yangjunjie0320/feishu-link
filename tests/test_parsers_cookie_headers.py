import httpx
import respx

from feishu_link.config import Settings
from feishu_link.parsers.og_meta import OGMetaParser


@respx.mock
async def test_og_parser_sends_platform_cookie_header(tmp_path) -> None:
    cookie_file = tmp_path / "zhihu.txt"
    cookie_file.write_text(
        ".zhihu.com\tTRUE\t/\tTRUE\t1800000000\tz_c0\tabc\n",
        encoding="utf-8",
    )
    settings = Settings(platform_cookie_files={"zhihu": str(cookie_file)})
    route = respx.get("https://www.zhihu.com/question/1").mock(
        return_value=httpx.Response(
            200,
            html="<html><head><title>知乎回答</title></head></html>",
        )
    )

    async with httpx.AsyncClient() as client:
        parser = OGMetaParser(client, settings)
        meta = await parser.parse("https://www.zhihu.com/question/1")

    assert meta.platform == "zhihu"
    assert route.calls.last.request.headers["Cookie"] == "z_c0=abc"
