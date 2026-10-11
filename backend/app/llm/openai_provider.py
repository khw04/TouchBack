"""OpenAI LLM Provider with token tracking and explicit credential enforcement."""
import json
from typing import Any
import httpx

from app.config import Settings
from app.llm.base import BaseLlmProvider, LlmResponse, LlmUsage, ToolCall


class MissingApiKeyError(Exception):
    """Raised when an API call is attempted without an API key configured."""

    pass


class OpenAiProvider(BaseLlmProvider):
    """Real OpenAI provider making external requests and tracking token usage."""

    def __init__(self, settings: Settings | None = None) -> None:
        from app.config import settings as default_settings

        self.settings = settings or default_settings
        self.api_url = "https://api.openai.com/v1/chat/completions"

    async def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LlmResponse:
        api_key = self.settings.openai_api_key.strip()
        if not api_key:
            raise MissingApiKeyError(
                "OPENAI_API_KEY가 설정되지 않았습니다. 자격증명 없이 실제 모델을 호출할 수 없습니다."
            )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        payload: dict[str, Any] = {
            "model": self.settings.openai_model,
            "messages": messages,
        }

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(self.api_url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()

        # Parse token usage
        usage_data = data.get("usage", {})
        prompt_tokens = usage_data.get("prompt_tokens", 0)
        completion_tokens = usage_data.get("completion_tokens", 0)
        total_tokens = usage_data.get("total_tokens", prompt_tokens + completion_tokens)
        reasoning_tokens = (
            usage_data.get("completion_tokens_details", {}).get("reasoning_tokens", 0)
        )

        usage = LlmUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            total_tokens=total_tokens,
        )

        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        content = message.get("content")

        tool_calls: list[ToolCall] = []
        raw_tool_calls = message.get("tool_calls", [])
        for raw in raw_tool_calls:
            fn = raw.get("function", {})
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except Exception:
                args = {}
            tool_calls.append(
                ToolCall(
                    id=raw.get("id", ""),
                    name=fn.get("name", ""),
                    arguments=args,
                )
            )

        return LlmResponse(
            text=content,
            tool_calls=tool_calls,
            usage=usage,
            model=data.get("model", self.settings.openai_model),
        )
