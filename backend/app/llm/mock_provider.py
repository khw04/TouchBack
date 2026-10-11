"""Deterministic Mock LLM Provider for offline testing and contract verification."""
from typing import Any
import uuid

from app.llm.base import BaseLlmProvider, LlmResponse, LlmUsage, ToolCall


class MockLlmProvider(BaseLlmProvider):
    """Mock provider with predictable responses without consuming API quota."""

    def __init__(self, default_model: str = "mock-gpt-6-luna") -> None:
        self.default_model = default_model
        self._queued_responses: list[LlmResponse] = []

    def queue_response(self, response: LlmResponse) -> None:
        self._queued_responses.append(response)

    def queue_tool_call(self, tool_name: str, arguments: dict[str, Any]) -> None:
        call = ToolCall(id=f"call-{uuid.uuid4().hex[:6]}", name=tool_name, arguments=arguments)
        self._queued_responses.append(
            LlmResponse(
                text=None,
                tool_calls=[call],
                usage=LlmUsage(prompt_tokens=20, completion_tokens=15, total_tokens=35),
                model=self.default_model,
            )
        )

    def queue_text(self, text: str) -> None:
        self._queued_responses.append(
            LlmResponse(
                text=text,
                tool_calls=[],
                usage=LlmUsage(prompt_tokens=15, completion_tokens=10, total_tokens=25),
                model=self.default_model,
            )
        )

    async def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LlmResponse:
        if self._queued_responses:
            return self._queued_responses.pop(0)

        # Default fallback response based on the last user message
        last_msg = messages[-1] if messages else {}
        content = str(last_msg.get("content", ""))

        if "올려" in content or "켜" in content or "increase" in content.lower():
            # Generate tool call to observe touch result if tools are provided
            if tools and any(t.get("function", {}).get("name") == "await_touch_result" for t in tools):
                return LlmResponse(
                    text=None,
                    tool_calls=[
                        ToolCall(
                            id=f"call-{uuid.uuid4().hex[:6]}",
                            name="await_touch_result",
                            arguments={"action_id": "act-mock", "window_ms": 3000},
                        )
                    ],
                    usage=LlmUsage(prompt_tokens=30, completion_tokens=18, total_tokens=48),
                    model=self.default_model,
                )
            return LlmResponse(
                text="오른쪽 화구의 + 버튼을 한 번 눌러 주세요.",
                tool_calls=[],
                usage=LlmUsage(prompt_tokens=25, completion_tokens=12, total_tokens=37),
                model=self.default_model,
            )

        return LlmResponse(
            text="요청하신 조작을 진행합니다.",
            tool_calls=[],
            usage=LlmUsage(prompt_tokens=10, completion_tokens=8, total_tokens=18),
            model=self.default_model,
        )
