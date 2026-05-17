import httpx
import pytest
import respx

from feishu_link.config import Settings
from feishu_link.parsers.base import LinkMetadata
from feishu_link.translator import TitleTranslator, contains_chinese


def test_contains_chinese() -> None:
    assert contains_chinese("中文标题") is True
    assert contains_chinese("English title") is False


async def test_translate_skips_chinese_title() -> None:
    settings = Settings(
        title_translation_enabled=True,
        deepseek_api_key="test-key",
    )
    meta = LinkMetadata(source_url="https://example.com", title="中文标题")
    async with httpx.AsyncClient() as client:
        translator = TitleTranslator(settings, client)
        await translator.translate_metadata(meta)

    assert meta.translated_title == ""


@respx.mock
async def test_translate_non_chinese_title() -> None:
    settings = Settings(
        title_translation_enabled=True,
        deepseek_api_key="test-key",
    )
    respx.post("https://api.deepseek.com/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": "超级反派来了"}}]},
        )
    )
    meta = LinkMetadata(
        source_url="https://www.tiktok.com/@u/video/123",
        title="Bro we boutta get some super villains",
    )

    async with httpx.AsyncClient() as client:
        translator = TitleTranslator(settings, client)
        await translator.translate_metadata(meta)

    assert meta.translated_title == "超级反派来了"


@pytest.mark.parametrize("text,expected", [
    ("\"中文标题\"", "中文标题"),
    ("  hello   world  ", "hello world"),
])
def test_clean_translation(text: str, expected: str) -> None:
    from feishu_link.translator import _clean_translation

    assert _clean_translation(text) == expected
