"""Tests for Agent configuration, tools registry, and LLM providers (C-YD-01)."""
import asyncio
import os
from pathlib import Path
import pytest

from app.config import Settings, load_env_file
from app.llm.base import LlmResponse, LlmUsage, ToolCall
from app.llm.factory import get_llm_provider
from app.llm.mock_provider import MockLlmProvider
from app.llm.openai_provider import MissingApiKeyError, OpenAiProvider
from app.tools.base import BaseTool, ToolRegistry, ToolResult
from app.tools.registry import default_tool_registry
from app.tools.sensor_tools import AwaitTouchResultTool, CheckSensorIntegrityTool, ReadFieldTool


def test_config_env_parser_and_precedence(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=test-file-key\nOPENAI_MODEL=test-model\n", encoding="utf-8")

    loaded = load_env_file(env_file)
    assert loaded["OPENAI_API_KEY"] == "test-file-key"
    assert loaded["OPENAI_MODEL"] == "test-model"

    # Environment variable priority
    old_env = os.environ.get("OPENAI_API_KEY")
    try:
        os.environ["OPENAI_API_KEY"] = "sys-key"
        settings = Settings.load()
        assert settings.openai_api_key == "sys-key"
    finally:
        if old_env is not None:
            os.environ["OPENAI_API_KEY"] = old_env
        else:
            os.environ.pop("OPENAI_API_KEY", None)


def test_sensor_tools_execution():
    async def _run():
        # 1. read_field
        tool_field = ReadFieldTool()
        res1 = await tool_field.execute(zone_id="right")
        assert res1.success is True
        assert res1.data["zone_id"] == "right"
        assert "rms" in res1.data

        bad_field = await tool_field.execute(zone_id="center")
        assert bad_field.success is False

        # 2. await_touch_result
        tool_touch = AwaitTouchResultTool()
        res2 = await tool_touch.execute(action_id="act-test", window_ms=2500)
        assert res2.success is True
        assert res2.data["action_id"] == "act-test"
        assert res2.data["window_ms"] == 2500

        bad_touch = await tool_touch.execute()
        assert bad_touch.success is False

        # 3. check_sensor_integrity
        tool_check = CheckSensorIntegrityTool()
        res3 = await tool_check.execute()
        assert res3.success is True
        assert res3.data["connected"] is True

    asyncio.run(_run())


def test_tool_registry_openai_schema():
    tools_schema = default_tool_registry.to_openai_tools()
    assert len(tools_schema) >= 3
    names = [t["function"]["name"] for t in tools_schema]
    assert "read_field" in names
    assert "await_touch_result" in names
    assert "check_sensor_integrity" in names


def test_mock_llm_provider_generation_and_usage():
    async def _run():
        mock = MockLlmProvider()

        # Default increase prompt generates tool call or instruction
        tools = default_tool_registry.to_openai_tools()
        res = await mock.generate(messages=[{"role": "user", "content": "오른쪽 화력 1단 올려줘"}], tools=tools)
        assert len(res.tool_calls) == 1
        assert res.tool_calls[0].name == "await_touch_result"
        assert res.usage.total_tokens > 0

        # Queued response works
        mock.queue_text("화구 설정이 완료되었습니다.")
        res2 = await mock.generate(messages=[{"role": "user", "content": "상태 알려줘"}])
        assert res2.text == "화구 설정이 완료되었습니다."
        assert res2.usage.total_tokens > 0

    asyncio.run(_run())


def test_openai_provider_fails_safely_when_key_missing():
    async def _run():
        empty_settings = Settings(openai_api_key="")
        provider = OpenAiProvider(settings=empty_settings)

        with pytest.raises(MissingApiKeyError) as exc_info:
            await provider.generate(messages=[{"role": "user", "content": "hello"}])
        assert "자격증명 없이 실제 모델을 호출할 수 없습니다" in str(exc_info.value)

    asyncio.run(_run())


def test_llm_factory_selection():
    mock_p = get_llm_provider(mode="mock_llm")
    assert isinstance(mock_p, MockLlmProvider)

    real_p = get_llm_provider(mode="real_llm")
    assert isinstance(real_p, OpenAiProvider)
