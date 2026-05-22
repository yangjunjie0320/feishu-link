import json

import httpx
import respx

from src.bibi_client import BibiClient
from src.config import Settings


@respx.mock
async def test_bibi_client_appends_fixed_markdown_bullet_prompt() -> None:
    route = respx.post("https://bibigpt.test/api/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "## Summary\n- Point"}}],
                "model": "bibigpt",
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 2,
                    "total_tokens": 3,
                },
            },
        )
    )
    settings = Settings(
        bibigpt_base_url="https://bibigpt.test",
        cookie_file="",
    )

    result = await BibiClient(settings).summarize(
        "https://youtu.be/abc123",
        prompt="Focus on the business implications.",
    )

    request = route.calls.last.request
    body = json.loads(request.content)
    text_parts = [
        part["text"]
        for part in body["messages"][0]["content"]
        if part["type"] == "text"
    ]
    prompt = text_parts[0]

    assert result.content == "## Summary\n- Point"
    assert "Focus on the business implications." in prompt
    assert "Do not use any emoji." in prompt
    assert "Use Markdown formatting." in prompt
    assert "Do not use Markdown headings" in prompt
    assert "Use nested bullet points only" in prompt
    assert 'Use "-" as the only unordered bullet marker' in prompt
    assert "Use four spaces for each nested bullet level" in prompt
    assert "Do not use numbered lists." in prompt
