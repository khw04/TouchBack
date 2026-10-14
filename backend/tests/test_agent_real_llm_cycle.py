"""Tests for Agent Multi-turn Tool Calling Loop, Token Tracking, and Error Handling (C-YD-04)."""
import asyncio
import json
import pytest

from app.adapters.mock_setup import build_mock_adapter
from app.agent.orchestrator import AgentOrchestrator, UsageSummary
from app.agent.recovery import MisoperationRecoverySkill, RecoveryDecisionType, RecoveryResult
from app.contracts.models import Goal, Instruction, Verdict
from app.llm.base import LlmResponse, LlmUsage, ToolCall
from app.llm.mock_provider import MockLlmProvider
from app.llm.openai_provider import (
    LlmAuthenticationError,
    LlmProviderError,
    LlmRateLimitError,
    LlmTimeoutError,
    MissingApiKeyError,
    OpenAiProvider,
)
from app.tools.base import BaseTool, ToolRegistry, ToolResult
from app.tools.registry import create_mock_registry
from app.tools.sensor_tools import MockSensorAdapter


def test_usage_summary_accumulation():
    usage = UsageSummary()
    assert usage.total_tokens == 0
    assert usage.call_count == 0

    # 1. First record with reasoning tokens
    u1 = LlmUsage(prompt_tokens=30, completion_tokens=20, reasoning_tokens=5, total_tokens=50)
    usage.record(u1, model="gpt-6-luna")
    assert usage.prompt_tokens == 30
    assert usage.completion_tokens == 20
    assert usage.reasoning_tokens == 5
    assert usage.total_tokens == 50
    assert usage.call_count == 1
    assert usage.model == "gpt-6-luna"

    # 2. Second record
    u2 = LlmUsage(prompt_tokens=15, completion_tokens=10, reasoning_tokens=0, total_tokens=25)
    usage.record(u2, model="gpt-6-luna")
    assert usage.total_tokens == 75
    assert usage.call_count == 2

    # 3. Formatted message check
    msg = usage.to_event_message()
    assert "model: gpt-6-luna" in msg
    assert "calls: 2" in msg
    assert "tokens: 75" in msg
    assert "reasoning: 5" in msg


def test_agent_orchestrator_multi_turn_tool_loop():
    """AgentOrchestrator가 LLM의 Tool 호출을 해석하고 실행한 뒤 최종 안내를 수립하는 Multi-turn 루프 검증."""
    async def _run():
        mock_llm = MockLlmProvider(default_model="mock-gpt-6-luna")
        adapter = MockSensorAdapter(source="mock")
        tools = create_mock_registry(adapter)

        # 1. Queue a tool call from LLM (check_sensor_integrity)
        call_id = "call-check-01"
        mock_llm.queue_response(
            LlmResponse(
                text=None,
                tool_calls=[ToolCall(id=call_id, name="check_sensor_integrity", arguments={})],
                usage=LlmUsage(prompt_tokens=40, completion_tokens=10, total_tokens=50),
                model="mock-gpt-6-luna",
            )
        )
        # 2. Queue final text response from LLM after receiving tool result
        mock_llm.queue_response(
            LlmResponse(
                text="센서 정상 확인되었습니다. 오른쪽 화구의 + 버튼을 한 번 눌러 주세요.",
                tool_calls=[],
                usage=LlmUsage(prompt_tokens=60, completion_tokens=25, total_tokens=85),
                model="mock-gpt-6-luna",
            )
        )

        orch = AgentOrchestrator(llm_provider=mock_llm, tool_registry=tools)
        goal, instruction, plan_msg, executed_tools = await orch.plan_and_instruct(
            user_input="오른쪽 화력 1단 올려줘",
            zone_id="right",
            action_id="act-test-multi",
        )

        assert goal is not None
        assert goal.zone_id == "right"
        assert instruction is not None
        assert instruction.action_id == "act-test-multi"
        assert "오른쪽 화구의 + 버튼" in instruction.text
        assert len(executed_tools) == 1
        assert executed_tools[0]["name"] == "check_sensor_integrity"
        assert executed_tools[0]["result"]["success"] is True

        # Token usage accumulated across 2 turns
        assert orch.usage.call_count == 2
        assert orch.usage.total_tokens == 135  # 50 + 85

    asyncio.run(_run())


def test_agent_orchestrator_blocks_unsupported_goal_before_llm():
    """P0 안전 게이트: 미지원 목표나 화구 미지정은 LLM 호출 없이 선행 차단."""
    async def _run():
        mock_llm = MockLlmProvider()
        orch = AgentOrchestrator(llm_provider=mock_llm)

        # 1. Unsupported direction
        goal1, inst1, err1, tools1 = await orch.plan_and_instruct("인덕션 꺼줘", zone_id="right")
        assert goal1 is None
        assert inst1 is None
        assert "지원하지 않거나 준비되지 않은 목표" in err1
        assert orch.usage.call_count == 0  # No LLM calls consumed

        # 2. Missing zone
        goal2, inst2, err2, tools2 = await orch.plan_and_instruct("화력 1단 올려줘", zone_id=None)
        assert goal2 is None
        assert inst2 is None
        assert "화구가 지정되지 않았습니다" in err2
        assert orch.usage.call_count == 0

    asyncio.run(_run())


def test_agent_orchestrator_replan_recovery_with_llm():
    """오조작 복구 시 LLM을 통한 재조작 안내 생성 및 토큰 집계 검증."""
    async def _run():
        mock_llm = MockLlmProvider()
        mock_llm.queue_text("출력 변화가 없습니다. 오른쪽 화구의 + 버튼을 다시 한 번 꼭 눌러 주세요.")

        orch = AgentOrchestrator(llm_provider=mock_llm)
        goal = Goal(zone_id="right", direction="increase", requested_steps=1)
        fallback_inst = Instruction(action_id="act-rec-01", text="오른쪽 화구의 + 버튼을 한 번 눌러 주세요.")
        rec_result = RecoveryResult(
            decision=RecoveryDecisionType.RETRY_INSTRUCTION,
            next_status="awaiting_user",
            instruction=fallback_inst,
            message="1회 재조작 안내",
        )

        inst, msg = await orch.replan_recovery_with_llm(goal, rec_result)
        assert inst is not None
        assert inst.action_id == "act-rec-01"
        assert "다시 한 번 꼭" in inst.text
        assert "LLM 재조작 안내 수립 완료" in msg
        assert orch.usage.call_count == 1
        assert orch.usage.total_tokens > 0

    asyncio.run(_run())


def test_openai_provider_exception_types():
    """OpenAiProvider 에러 세분화 검증 (MissingKey, Auth, RateLimit, Timeout)."""
    assert issubclass(MissingApiKeyError, Exception)
    assert issubclass(LlmAuthenticationError, Exception)
    assert issubclass(LlmRateLimitError, Exception)
    assert issubclass(LlmTimeoutError, Exception)
    assert issubclass(LlmProviderError, Exception)
