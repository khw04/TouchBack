"""LLM Provider factory based on execution mode and credentials."""
from app.config import settings
from app.llm.base import BaseLlmProvider
from app.llm.mock_provider import MockLlmProvider
from app.llm.openai_provider import OpenAiProvider


def get_llm_provider(mode: str | None = None) -> BaseLlmProvider:
    """Return appropriate LLM provider. Defaults to MockLlmProvider if key is missing or mode is mock."""
    target_mode = mode or settings.execution_mode
    if target_mode == "real_llm":
        return OpenAiProvider(settings)
    return MockLlmProvider()
