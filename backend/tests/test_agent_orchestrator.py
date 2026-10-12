"""Tests for Agent Orchestrator and Verifier (C-YD-02)."""
import asyncio
import pytest

from app.agent.orchestrator import AgentOrchestrator
from app.agent.verifier import IntentVerifier
from app.contracts.models import Goal
from app.tools.base import ToolRegistry
from app.tools.sensor_tools import (
    AwaitTouchResultTool,
    CheckSensorIntegrityTool,
    MockSensorAdapter,
    ReadFieldTool,
)


def test_orchestrator_goal_interpretation():
    orch = AgentOrchestrator()

    # 1. Valid goal with zone
    goal1, err1 = orch.interpret_goal(user_input="오른쪽 화력 1단 올려줘", zone_id="right")
    assert goal1 is not None
    assert goal1.zone_id == "right"
    assert goal1.requested_steps == 1
    assert err1 is None

    # 2. Unsupported goal
    goal2, err2 = orch.interpret_goal(user_input="인덕션 꺼줘", zone_id="right")
    assert goal2 is None
    assert "지원하지 않거나 준비되지 않은 목표" in (err2 or "")

    # 3. Missing zone
    goal3, err3 = orch.interpret_goal(user_input="화력 1단 올려줘", zone_id=None)
    assert goal3 is None
    assert "화구가 지정되지 않았습니다" in (err3 or "")


def test_orchestrator_instruction_generation():
    orch = AgentOrchestrator()
    goal = Goal(zone_id="left", direction="increase", requested_steps=1)
    instruction = orch.build_instruction(goal, action_id="act-test-1")

    assert instruction.action_id == "act-test-1"
    assert instruction.text == "왼쪽 화구의 + 버튼을 한 번 눌러 주세요."


def test_orchestrator_scenario_exact_step_success():
    """시나리오 1: 유효한 1단계 화력 증가 성공 (exact_step / success / succeeded)."""
    async def _run():
        adapter = MockSensorAdapter(default_field_delta=45.2)
        registry = ToolRegistry()
        registry.register(AwaitTouchResultTool(provider=adapter.await_touch_result))

        orch = AgentOrchestrator(tool_registry=registry)
        goal = Goal(zone_id="right", direction="increase", requested_steps=1)

        verdict, data, feedback = await orch.execute_observation_and_verification(
            goal=goal,
            action_id="act-succ",
        )

        assert verdict.status == "success"
        assert verdict.verification_level == "exact_step"
        assert verdict.goal_satisfied is True
        assert orch.determine_next_status(verdict) == "succeeded"
        assert "화력이 한 단계 올라갔습니다" in feedback

    asyncio.run(_run())


def test_orchestrator_scenario_output_change_only():
    """시나리오 2: 출력 증가는 감지되었으나 단계 판별 불가 (output_change / uncertain)."""
    async def _run():
        # delta 15.0, beep 0 -> 단순 출력 증가
        def mock_provider(action_id: str, window_ms: int):
            return {
                "action_id": action_id,
                "window_ms": window_ms,
                "field_delta_rms": 15.0,
                "beep_count": 0,
                "status": "valid",
            }

        registry = ToolRegistry()
        registry.register(AwaitTouchResultTool(provider=mock_provider))

        orch = AgentOrchestrator(tool_registry=registry)
        goal = Goal(zone_id="right", direction="increase", requested_steps=1)

        verdict, data, feedback = await orch.execute_observation_and_verification(
            goal=goal,
            action_id="act-output-only",
        )

        assert verdict.status == "uncertain"
        assert verdict.verification_level == "output_change"
        assert verdict.goal_satisfied is None
        assert orch.determine_next_status(verdict) == "uncertain"
        assert "출력 증가를 확인했습니다. 정확히 한 단계인지는 확인하지 못했습니다" in feedback

    asyncio.run(_run())


def test_orchestrator_scenario_no_change_recovering():
    """시나리오 3: 유효한 시간창에서 변화 없음 (none / mismatch / recovering 후보)."""
    async def _run():
        def mock_no_change(action_id: str, window_ms: int):
            return {
                "action_id": action_id,
                "window_ms": window_ms,
                "field_delta_rms": 0.5,
                "beep_count": 0,
                "status": "valid",
            }

        registry = ToolRegistry()
        registry.register(AwaitTouchResultTool(provider=mock_no_change))

        orch = AgentOrchestrator(tool_registry=registry)
        goal = Goal(zone_id="left", direction="increase", requested_steps=1)

        verdict, data, feedback = await orch.execute_observation_and_verification(
            goal=goal,
            action_id="act-no-change",
        )

        assert verdict.status == "mismatch"
        assert verdict.verification_level == "none"
        assert verdict.goal_satisfied is False
        assert orch.determine_next_status(verdict) == "recovering"
        assert "출력 변화가 확인되지 않았습니다" in feedback

    asyncio.run(_run())


def test_orchestrator_scenario_sensor_quality_degraded():
    """시나리오 4: 센서 품질 불량 또는 비보정 (none / uncertain)."""
    async def _run():
        def mock_degraded(action_id: str, window_ms: int):
            return {
                "action_id": action_id,
                "window_ms": window_ms,
                "field_delta_rms": 45.0,
                "status": "degraded",
                "quality": "degraded",
            }

        registry = ToolRegistry()
        registry.register(AwaitTouchResultTool(provider=mock_degraded))

        orch = AgentOrchestrator(tool_registry=registry)
        goal = Goal(zone_id="right", direction="increase", requested_steps=1)

        verdict, data, feedback = await orch.execute_observation_and_verification(
            goal=goal,
            action_id="act-degraded",
        )

        assert verdict.status == "uncertain"
        assert verdict.verification_level == "none"
        assert verdict.goal_satisfied is None
        assert orch.determine_next_status(verdict) == "uncertain"
        assert "결과를 확인할 수 없습니다" in feedback

    asyncio.run(_run())
