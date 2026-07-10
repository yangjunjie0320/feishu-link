import json

import httpx
import pytest
import respx

from src.bibi_models import SubtitleSegment
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


@respx.mock
async def test_format_subtitles_uses_strict_json_and_preserves_timing() -> None:
    settings = Settings(
        deepseek_api_key="test-key",
        deepseek_thinking_enabled=True,
        deepseek_reasoning_effort="high",
        summary_rewrite_timeout=37.0,
    )
    route = respx.post("https://api.deepseek.com/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"items": [{"id": 7, "text": "这是  格式化后的字幕。"}]},
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )
    )
    original = SubtitleSegment(
        index=7,
        start_time=12.5,
        end_time=17.25,
        text="This is the subtitle",
        speaker_id=3,
    )

    async with httpx.AsyncClient() as client:
        formatted = await TitleTranslator(settings, client).format_subtitles(
            [original], content_id="content-1"
        )

    assert formatted == (
        SubtitleSegment(
            index=7,
            start_time=12.5,
            end_time=17.25,
            text="这是 格式化后的字幕。",
            speaker_id=3,
        ),
    )
    body = json.loads(route.calls.last.request.content)
    assert body["response_format"] == {"type": "json_object"}
    assert body["thinking"] == {"type": "disabled"}
    assert body["max_tokens"] == 8192
    user_prompt = body["messages"][1]["content"]
    input_data = json.loads(user_prompt.split("\n", maxsplit=1)[1])
    assert input_data == {"items": [{"id": 7, "text": "This is the subtitle"}]}
    assert "start_time" not in user_prompt
    assert "end_time" not in user_prompt
    assert "speaker_id" not in user_prompt
    assert route.calls.last.request.extensions["timeout"] == {
        "connect": 37.0,
        "read": 37.0,
        "write": 37.0,
        "pool": 37.0,
    }


@respx.mock
async def test_format_subtitles_batches_by_item_and_character_limits() -> None:
    settings = Settings(deepseek_api_key="test-key")
    batch_sizes: list[int] = []
    batch_characters: list[int] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        user_prompt = body["messages"][1]["content"]
        items = json.loads(user_prompt.split("\n", maxsplit=1)[1])["items"]
        batch_sizes.append(len(items))
        batch_characters.append(sum(len(item["text"]) for item in items))
        formatted = {
            "items": [{"id": item["id"], "text": f"格式化 {item['id']}"} for item in items]
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(formatted)}}]},
        )

    respx.post("https://api.deepseek.com/chat/completions").mock(side_effect=respond)
    subtitles = [
        SubtitleSegment(index=index, start_time=index, end_time=index + 1, text="a" * 75)
        for index in range(81)
    ]

    async with httpx.AsyncClient() as client:
        formatted = await TitleTranslator(settings, client).format_subtitles(subtitles)

    assert batch_sizes == [80, 1]
    assert batch_characters == [6000, 75]
    assert [segment.index for segment in formatted] == list(range(81))
    assert formatted[80].text == "格式化 80"


@respx.mock
async def test_format_subtitles_retries_invalid_batch_then_continues() -> None:
    settings = Settings(deepseek_api_key="test-key")
    request_number = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal request_number
        request_number += 1
        body = json.loads(request.content)
        items = json.loads(body["messages"][1]["content"].split("\n", maxsplit=1)[1])[
            "items"
        ]
        if request_number <= 2:
            output_items = list(reversed(items))
        else:
            output_items = [{"id": item["id"], "text": "最后一条已格式化"} for item in items]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps({"items": output_items})}}
                ]
            },
        )

    route = respx.post("https://api.deepseek.com/chat/completions").mock(side_effect=respond)
    subtitles = [
        SubtitleSegment(index=index, start_time=index, end_time=index + 1, text=f"original {index}")
        for index in range(81)
    ]

    async with httpx.AsyncClient() as client:
        formatted = await TitleTranslator(settings, client).format_subtitles(subtitles)

    assert route.call_count == 3
    assert formatted[:80] == tuple(subtitles[:80])
    assert formatted[80].text == "最后一条已格式化"


@respx.mock
async def test_format_subtitles_stops_after_service_error() -> None:
    settings = Settings(deepseek_api_key="test-key")
    route = respx.post("https://api.deepseek.com/chat/completions").mock(
        return_value=httpx.Response(401, json={"error": {"message": "invalid key"}})
    )
    subtitles = [
        SubtitleSegment(index=index, start_time=index, end_time=index + 1, text=f"original {index}")
        for index in range(81)
    ]

    async with httpx.AsyncClient() as client:
        formatted = await TitleTranslator(settings, client).format_subtitles(subtitles)

    assert route.call_count == 1
    assert formatted == tuple(subtitles)


@respx.mock
async def test_format_subtitles_falls_back_without_api_key() -> None:
    settings = Settings(deepseek_api_key="")
    route = respx.post("https://api.deepseek.com/chat/completions")
    subtitles = (SubtitleSegment(index=0, start_time=0, end_time=1, text="original"),)

    async with httpx.AsyncClient() as client:
        formatted = await TitleTranslator(settings, client).format_subtitles(subtitles)

    assert route.called is False
    assert formatted == subtitles


@respx.mock
async def test_format_subtitles_does_not_send_oversized_item() -> None:
    settings = Settings(deepseek_api_key="test-key")

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        items = json.loads(body["messages"][1]["content"].split("\n", maxsplit=1)[1])[
            "items"
        ]
        output = {"items": [{"id": item["id"], "text": "已格式化"} for item in items]}
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(output)}}]},
        )

    route = respx.post("https://api.deepseek.com/chat/completions").mock(side_effect=respond)
    subtitles = (
        SubtitleSegment(index=0, start_time=0, end_time=1, text="normal"),
        SubtitleSegment(index=1, start_time=1, end_time=2, text="x" * 6001),
    )

    async with httpx.AsyncClient() as client:
        formatted = await TitleTranslator(settings, client).format_subtitles(subtitles)

    assert route.call_count == 1
    assert formatted[0].text == "已格式化"
    assert formatted[1] == subtitles[1]
