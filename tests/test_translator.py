import json

import httpx
import pytest
import respx

from src.config import Settings
from src.parsers.base import LinkMetadata
from src.translator import TitleTranslator, contains_chinese


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


async def test_translate_skips_generic_social_title() -> None:
    settings = Settings(
        title_translation_enabled=True,
        deepseek_api_key="test-key",
    )
    meta = LinkMetadata(
        source_url="https://www.instagram.com/p/abc/",
        title="Instagram Reel",
        platform="instagram",
    )
    async with httpx.AsyncClient() as client:
        translator = TitleTranslator(settings, client)
        await translator.translate_metadata(meta)

    assert meta.translated_title == ""


async def test_translate_skips_x_post_title() -> None:
    settings = Settings(
        title_translation_enabled=True,
        deepseek_api_key="test-key",
    )
    meta = LinkMetadata(
        source_url="https://x.com/example/status/123",
        title="X Post",
        platform="x",
    )
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


@respx.mock
async def test_translate_non_chinese_description() -> None:
    settings = Settings(
        title_translation_enabled=True,
        deepseek_api_key="test-key",
    )
    respx.post("https://api.deepseek.com/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": "BMW M1 宽体车, 覆盖定制水钻艺术装饰"}}]},
        )
    )
    meta = LinkMetadata(
        source_url="https://www.instagram.com/p/abc/",
        title="Instagram Post",
        description="BMW M1 widebody, covered in custom rhinestone artwork",
        platform="instagram",
    )

    async with httpx.AsyncClient() as client:
        translator = TitleTranslator(settings, client)
        await translator.translate_metadata(meta)

    assert meta.translated_title == ""
    assert meta.translated_description == "BMW M1 宽体车, 覆盖定制水钻艺术装饰"


@pytest.mark.parametrize("text,expected", [
    ("\"中文标题\"", "中文标题"),
    ("  hello   world  ", "hello world"),
])
def test_clean_translation(text: str, expected: str) -> None:
    from src.translator import _clean_translation

    assert _clean_translation(text) == expected


def test_clean_markdown_translation_preserves_nested_list_indentation() -> None:
    from src.translator import _clean_markdown_translation

    text = (
        "- **问题**\n"
        "  - **大学应如何调整课程？** ：  使用 AI 作为研究工具\n"
        "    - 保持第一性原理"
    )

    assert _clean_markdown_translation(text) == (
        "- **问题**\n"
        "  - **大学应如何调整课程？** ： 使用 AI 作为研究工具\n"
        "    - 保持第一性原理"
    )


@respx.mock
async def test_ensure_chinese_markdown_summary_translates_english() -> None:
    settings = Settings(deepseek_api_key="test-key")
    route = respx.post("https://api.deepseek.com/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": "- 中文要点\n- 第二点"}}]},
        )
    )

    async with httpx.AsyncClient() as client:
        translated = await TitleTranslator(settings, client).ensure_chinese_markdown_summary(
            "- English point\n- Second point",
            source_url="https://youtu.be/abc",
            rewrite_prompt="重点保留商业影响。",
        )

    assert route.called
    body = json.loads(route.calls.last.request.content)
    user_prompt = body["messages"][1]["content"]
    assert "重点保留商业影响。" in user_prompt
    assert "BibiGPT 原始返回" in user_prompt
    assert "- English point" in user_prompt
    assert translated == "- 中文要点\n- 第二点"


@respx.mock
async def test_ensure_chinese_markdown_summary_rewrites_existing_chinese() -> None:
    settings = Settings(deepseek_api_key="test-key")
    route = respx.post("https://api.deepseek.com/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": "- 已改写为最终中文"}}]},
        )
    )

    async with httpx.AsyncClient() as client:
        translated = await TitleTranslator(settings, client).ensure_chinese_markdown_summary(
            "- 已经是中文",
            source_url="https://youtu.be/abc",
        )

    assert route.called
    body = json.loads(route.calls.last.request.content)
    system_prompt = body["messages"][0]["content"]
    user_prompt = body["messages"][1]["content"]
    assert "不要信任输入的语言和排版" in system_prompt
    assert "输出要求:" in user_prompt
    assert "必须用简体中文输出, 非中文内容要翻译成中文。" in user_prompt
    assert "不要使用 emoji。" in user_prompt
    assert "使用 Markdown，尽量保留原有结构。" in user_prompt
    assert "Markdown 各级标题都改成无序列表+加粗。" in user_prompt
    assert "允许使用多级无序列表，用缩进表达层级。" in user_prompt
    assert '无序列表只能使用 "-", 不要使用 "*" 或 "+"。' in user_prompt
    assert "不要使用编号列表。" in user_prompt
    assert "- 已经是中文" in user_prompt
    assert translated == "- 已改写为最终中文"
