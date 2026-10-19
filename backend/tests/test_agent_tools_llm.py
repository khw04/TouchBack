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
from app.tools.registry import (
    create_mock_registry,
    create_registry_from_adapter,
    default_tool_registry,
)
from app.tools.sensor_tools import (
    AwaitTouchResultTool,
    CheckSensorIntegrityTool,
    MockSensorAdapter,
    ReadFieldTool,
    create_mock_sensor_tools,
)


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


def test_sensor_tools_unconfigured_error():
    """Provider가 설정되지 않은 센서 도구는 고정 성공 대신 명시적 오류/불확실을 반환해야 함 (P1 피드백)."""
    async def _run():
        # 1. read_field without provider
        tool_field = ReadFieldTool()
        res1 = await tool_field.execute(zone_id="right")
        assert res1.success is False
        assert "센서 어댑터가 설정되지 않았거나" in (res1.error or "")

        bad_field = await tool_field.execute(zone_id="center")
        assert bad_field.success is False

        # 2. await_touch_result without provider
        tool_touch = AwaitTouchResultTool()
        res2 = await tool_touch.execute(action_id="act-test", window_ms=2500)
        assert res2.success is False
        assert "센서 어댑터가 설정되지 않았거나" in (res2.error or "")

        # 3. check_sensor_integrity without provider
        tool_check = CheckSensorIntegrityTool()
        res3 = await tool_check.execute()
        assert res3.success is False
        assert res3.data["connected"] is False
        assert res3.data["calibrated"] is False
        assert res3.data["clock_sync"] == "unsynced"

    asyncio.run(_run())


def test_mock_sensor_tools_execution():
    """명시적인 MockSensorAdapter를 주입했을 때의 정상 모의 관측 동작 검증."""
    async def _run():
        adapter = MockSensorAdapter(source="mock")
        tools = create_mock_sensor_tools(adapter)
        field_tool, touch_tool, check_tool = tools[0], tools[1], tools[2]

        # 1. read_field with mock adapter
        res1 = await field_tool.execute(zone_id="right")
        assert res1.success is True
        assert res1.data["zone_id"] == "right"
        assert res1.data["source"] == "mock"
        assert "rms" in res1.data

        # 2. await_touch_result with mock adapter
        res2 = await touch_tool.execute(action_id="act-test", window_ms=2500)
        assert res2.success is True
        assert res2.data["action_id"] == "act-test"
        assert res2.data["window_ms"] == 2500
        assert res2.data["source"] == "mock"

        # 3. check_sensor_integrity with mock adapter
        res3 = await check_tool.execute()
        assert res3.success is True
        assert res3.data["connected"] is True
        assert res3.data["calibrated"] is True
        assert res3.data["clock_sync"] == "verified"
        assert res3.data["source"] == "mock"

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


def test_llm_factory_selection(monkeypatch):
    from app.config import settings
    from app.llm.claude_provider import ClaudeProvider
    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    assert isinstance(get_llm_provider(mode="real_llm"), ClaudeProvider)
    monkeypatch.setattr(settings, "llm_provider", "openai")
    mock_p = get_llm_provider(mode="mock_llm")
    assert isinstance(mock_p, MockLlmProvider)

    real_p = get_llm_provider(mode="real_llm")
    assert isinstance(real_p, OpenAiProvider)


def test_create_registry_from_adapter_planning_and_execution():
    """[P1 회귀] 계획 단계 LLM에는 await_touch_result가 노출되지 않고, 실행 단계에서는 실제 어댑터를 호출해 관측·시계가 갱신되어야 함."""
    from app.adapters.mock_setup import build_mock_adapter
    from app.adapters.mock import MockAdapter, VirtualClock

    async def _run():
        adapter_normal = build_mock_adapter("normal")

        # 1. 계획 단계: for_planning=True 시 await_touch_result 도구 미제공 검증
        reg_plan = create_registry_from_adapter(adapter_normal, for_planning=True)
        plan_tools = reg_plan.to_openai_tools()
        tool_names = [t["function"]["name"] for t in plan_tools]
        assert "read_field" in tool_names
        assert "check_sensor_integrity" in tool_names
        assert "await_touch_result" not in tool_names

        # 2. 실행 단계: for_planning=False 시 실제 adapter.await_touch_result 호출 및 가상 시계 전진 검증
        reg_exec = create_registry_from_adapter(adapter_normal, for_planning=False)
        touch_tool = reg_exec.get("await_touch_result")
        assert touch_tool is not None

        clock_before = adapter_normal.clock.now_ms
        res_normal = await touch_tool.execute(action_id="act-exec-1", window_ms=3000)
        clock_after = adapter_normal.clock.now_ms
        assert clock_after == clock_before + 3000  # 가상 시계 3000ms 전진
        assert res_normal.success is True
        # normal 어댑터이므로 실제 도착한 관측의 비프음 반영
        assert res_normal.data["beep_count"] >= 1
        assert res_normal.data["field_delta_rms"] > 0.0

        # 3. no_change 어댑터: 고정 가짜값(1비프, 45.2)이 아니라 실제 관측 부재(0비프, 0.0 RMS) 반영 검증
        adapter_nc = build_mock_adapter("no_change")
        reg_nc = create_registry_from_adapter(adapter_nc, for_planning=False)
        touch_tool_nc = reg_nc.get("await_touch_result")
        res_nc = await touch_tool_nc.execute(action_id="act-nc-1", window_ms=3000)
        assert res_nc.success is True
        assert res_nc.data["beep_count"] == 0
        assert res_nc.data["field_delta_rms"] == 0.0
        assert res_nc.data["status"] == "valid"

        # 4. 빈 어댑터: missing_samples 반환
        adapter_empty = MockAdapter([], VirtualClock(now_ms=500))
        reg_empty = create_registry_from_adapter(adapter_empty, for_planning=False)
        touch_tool_empty = reg_empty.get("await_touch_result")
        res_empty = await touch_tool_empty.execute(action_id="act-empty-1", window_ms=3000)
        assert res_empty.success is True
        assert res_empty.data["beep_count"] == 0
        assert res_empty.data["field_delta_rms"] == 0.0
        assert res_empty.data["status"] == "missing_samples"

    asyncio.run(_run())
