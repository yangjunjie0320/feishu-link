import httpx
import respx

from src.parsers.x_oembed import XOEmbedParser, canonical_x_status_url


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


@respx.mock
async def test_x_oembed_strips_media_subpath_before_request() -> None:
    route = respx.get("https://publish.twitter.com/oembed").mock(
        return_value=httpx.Response(
            200,
            json={
                "url": "https://x.com/Saccc_c/status/2070779893197987919",
                "author_name": "Sac",
                "author_url": "https://x.com/Saccc_c",
                "html": '<blockquote><p>正文</p></blockquote>',
            },
        )
    )

    async with httpx.AsyncClient() as client:
        meta = await XOEmbedParser(client).parse(
            "https://x.com/Saccc_c/status/2070779893197987919/video/1?s=46"
        )

    request_url = route.calls.last.request.url
    assert request_url.params["url"] == (
        "https://x.com/Saccc_c/status/2070779893197987919?s=46"
    )
    assert meta.description == "正文"


def test_canonical_x_status_url_keeps_query_and_strips_tail() -> None:
    assert canonical_x_status_url(
        "https://x.com/Saccc_c/status/2070779893197987919/video/1?s=46#frag"
    ) == "https://x.com/Saccc_c/status/2070779893197987919?s=46"
