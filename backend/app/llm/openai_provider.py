"""OpenAI LLM Provider with token tracking and explicit credential enforcement."""
import json
from typing import Any
import httpx

from app.config import Settings
from app.llm.base import BaseLlmProvider, LlmResponse, LlmUsage, ToolCall


class MissingApiKeyError(Exception):
    """Raised when an API call is attempted without an API key configured."""

    pass


class LlmAuthenticationError(Exception):
    """Raised when the LLM provider returns a 401 Unauthorized status."""

    pass


class LlmRateLimitError(Exception):
    """Raised when the LLM provider returns a 429 Rate Limit / Quota Exceeded status."""

    pass


class LlmTimeoutError(Exception):
    """Raised when an LLM provider request times out."""

    pass


class LlmProviderError(Exception):
    """Raised when an unexpected error occurs during LLM provider communication."""

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

        if getattr(self.settings, "openai_reasoning_effort", None):
            payload["reasoning_effort"] = self.settings.openai_reasoning_effort

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(self.api_url, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
        except httpx.TimeoutException as exc:
            raise LlmTimeoutError("OpenAI API 호출 시간이 초과되었습니다 (30초).") from exc
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code == 401:
                raise LlmAuthenticationError(
                    "OpenAI API 인증에 실패했습니다 (401 Unauthorized). API 키를 확인해 주세요."
                ) from exc
            if status_code == 429:
                raise LlmRateLimitError(
                    "OpenAI API 사용 한도 초과 또는 요청 제한에 도달했습니다 (429 Rate Limit)."
                ) from exc
            raise LlmProviderError(
                f"OpenAI API 통신 오류 발생 (HTTP {status_code}): {exc.response.text}"
            ) from exc
        except httpx.RequestError as exc:
            raise LlmProviderError(
                f"OpenAI API 요청 네트워크 오류 발생: {str(exc)}"
            ) from exc

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
