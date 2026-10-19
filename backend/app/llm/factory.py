"""LLM Provider factory based on execution mode and credentials."""
from app.config import settings
from app.llm.base import BaseLlmProvider
from app.llm.mock_provider import MockLlmProvider
from app.llm.openai_provider import OpenAiProvider
from app.llm.claude_provider import ClaudeProvider


def get_llm_provider(mode: str | None = None) -> BaseLlmProvider:
    """Return appropriate LLM provider. Missing credentials never fall back to Mock in real mode."""
    target_mode = mode or settings.execution_mode
    if target_mode == "real_llm":
        return ClaudeProvider(settings) if settings.llm_provider == "anthropic" else OpenAiProvider(settings)
    return MockLlmProvider()
