"""LLM Provider abstractions, token usage tracking, and message contracts."""
from abc import ABC, abstractmethod
from typing import Any
from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class LlmUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0


class LlmResponse(BaseModel):
    text: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: LlmUsage = Field(default_factory=LlmUsage)
    model: str = "mock-model"


class BaseLlmProvider(ABC):
    """Abstract interface for LLM backends."""

    @abstractmethod
    async def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LlmResponse:
        """Generate model response with optional tool calls."""
        pass
