import httpx
import respx

from src.parsers.x_oembed import XOEmbedParser


@respx.mock
async def test_x_oembed_extracts_post_text_and_author() -> None:
    route = respx.get("https://publish.twitter.com/oembed").mock(
        return_value=httpx.Response(
            200,
            json={
                "url": "https://twitter.com/NFTCPS/status/2056556650123956505",
                "author_name": "Example Author",
                "author_url": "https://twitter.com/NFTCPS",
                "html": (
                    '<blockquote class="twitter-tweet">'
                    '<p lang="zh" dir="ltr">正文 第一行<br>第二行 '
                    '<a href="https://t.co/example">https://t.co/example</a></p>'
                    "</blockquote>"
                ),
            },
        )
    )

    async with httpx.AsyncClient() as client:
        meta = await XOEmbedParser(client).parse(
            "https://x.com/NFTCPS/status/2056556650123956505"
        )

    assert route.called
    assert meta.title == "Post by @NFTCPS"
    assert meta.channel == "@NFTCPS"
    assert meta.description == "正文 第一行 第二行 https://t.co/example"
    assert meta.site_name == "X"
    assert meta.platform == "x"
