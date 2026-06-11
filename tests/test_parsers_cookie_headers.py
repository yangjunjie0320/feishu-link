import httpx
import respx

from src.config import Settings
from src.parsers.og_meta import OGMetaParser


@respx.mock
async def test_og_parser_sends_platform_cookie_header(tmp_path) -> None:
    cookie_file = tmp_path / "bilibili.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tTRUE\t1800000000\tSESSDATA\tabc\n",
        encoding="utf-8",
    )
    settings = Settings(cookie_file=str(cookie_file))
    route = respx.get("https://www.bilibili.com/read/cv1").mock(
        return_value=httpx.Response(
            200,
            html="<html><head><title>B站专栏</title></head></html>",
        )
    )

    async with httpx.AsyncClient() as client:
        parser = OGMetaParser(client, settings)
        meta = await parser.parse("https://www.bilibili.com/read/cv1")

    assert meta.platform == "bilibili"
    assert route.calls.last.request.headers["Cookie"] == "SESSDATA=abc"
