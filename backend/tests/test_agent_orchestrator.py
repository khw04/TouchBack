"""Tests for Agent Orchestrator and Verifier (C-YD-02)."""
import asyncio
import pytest

from app.adapters.mock_setup import build_mock_adapter
from app.agent.orchestrator import AgentOrchestrator
from app.agent.verifier import IntentVerifier
from app.contracts.models import Goal
from app.verification.models import ActionContext, MockCalibration


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
    """시나리오 1: 정상 Mock 어댑터를 통한 유효한 1단계 화력 증가 성공 (exact_step / success / succeeded)."""
    async def _run():
        adapter = build_mock_adapter("normal", zone_id="right")
        orch = AgentOrchestrator(adapter=adapter)
        goal = Goal(zone_id="right", direction="increase", requested_steps=1)

        verdict, feedback = await orch.execute_observation_and_verification(
            goal=goal,
            action_id="act-succ",
            run_id="run-succ",
        )

        assert verdict.status == "success"
        assert verdict.verification_level == "exact_step"
        assert verdict.goal_satisfied is True
        assert orch.determine_next_status(verdict) == "succeeded"
        assert "화력이 한 단계 올라갔습니다" in feedback
        # 실제 관측 ID 보존 확인
        assert len(verdict.evidence_ids) >= 2

    asyncio.run(_run())


def test_orchestrator_scenario_no_change_recovering():
    """시나리오 2: 유효한 시간창에서 변화 없음 (none / mismatch / recovering 후보)."""
    async def _run():
        adapter = build_mock_adapter("no_change", zone_id="right")
        orch = AgentOrchestrator(adapter=adapter)
        goal = Goal(zone_id="right", direction="increase", requested_steps=1)

        verdict, feedback = await orch.execute_observation_and_verification(
            goal=goal,
            action_id="act-no-change",
            run_id="run-no-change",
        )

        assert verdict.status == "mismatch"
        assert verdict.verification_level == "none"
        assert verdict.goal_satisfied is False
        assert "no_output_change" in verdict.reason_codes
        assert orch.determine_next_status(verdict) == "recovering"
        assert "출력 변화가 확인되지 않았습니다" in feedback

    asyncio.run(_run())


def test_orchestrator_scenario_missing_observations_becomes_uncertain():
    """시나리오 3: 관측 누락({})은 유효한 변화 없음이 아니므로 uncertain으로 종결되고 recovering 진입 차단."""
    async def _run():
        adapter = build_mock_adapter("missing", zone_id="right")
        orch = AgentOrchestrator(adapter=adapter)
        goal = Goal(zone_id="right", direction="increase", requested_steps=1)

        verdict, feedback = await orch.execute_observation_and_verification(
            goal=goal,
            action_id="act-missing",
            run_id="run-missing",
        )

        assert verdict.status == "uncertain"
        assert verdict.verification_level == "none"
        assert "observations_missing" in verdict.reason_codes
        # 누락은 유효한 변화 없음과 구분되어 uncertain 종결
        assert orch.determine_next_status(verdict) == "uncertain"
        assert "결과를 확인할 수 없습니다" in feedback

    asyncio.run(_run())


def test_orchestrator_scenario_sensor_error_becomes_uncertain():
    """시나리오 4: 센서 품질 불량(sensor_error)은 uncertain 안전 중단."""
    async def _run():
        adapter = build_mock_adapter("sensor_error", zone_id="right")
        orch = AgentOrchestrator(adapter=adapter)
        goal = Goal(zone_id="right", direction="increase", requested_steps=1)

        verdict, feedback = await orch.execute_observation_and_verification(
            goal=goal,
            action_id="act-faulty",
            run_id="run-faulty",
        )

        assert verdict.status == "uncertain"
        assert verdict.verification_level == "none"
        assert "sensor_quality_invalid" in verdict.reason_codes
        assert orch.determine_next_status(verdict) == "uncertain"
        assert "결과를 확인할 수 없습니다" in feedback

    asyncio.run(_run())


def test_verifier_rejects_zone_mismatch_and_preserves_contract():
    """리뷰어 지적사항 1 검증: 목표가 right인데 관측이 left인 경우 verify_intent 계약에 의해 거부."""
    adapter = build_mock_adapter("normal", zone_id="right")
    baseline = adapter.read_baseline("right")
    observations = adapter.await_touch_result(
        run_id="run-1",
        action_id="act-mismatch",
        zone_id="right",
        window_start_ms=500,
        window_end_ms=3500,
    )
    # 목표는 right인데 관측 프레임의 zone_id를 left로 변조
    observations[0].device_observation.zone_id = "left"

    context = ActionContext(
        run_id="run-1",
        action_id="act-mismatch",
        window_start_ms=500,
        window_end_ms=3500,
        now_ms=3500,
    )
    goal = Goal(zone_id="right", direction="increase", requested_steps=1)
    verifier = IntentVerifier()
    verdict = verifier.verify(goal, baseline, observations, context)

    assert verdict.status == "uncertain"
    assert "zone_mismatch" in verdict.reason_codes
    assert verdict.goal_satisfied is None


def test_verifier_rejects_uncalibrated_or_unverified_clock():
    """리뷰어 지적사항 1 검증: 시계 미동기화 및 품질 불량 관측에 대해 exact_step success 허용 안 함."""
    adapter = build_mock_adapter("normal", zone_id="right")
    baseline = adapter.read_baseline("right")
    observations = adapter.await_touch_result(
        run_id="run-1",
        action_id="act-1",
        zone_id="right",
        window_start_ms=500,
        window_end_ms=3500,
    )
    # 시계 미동기화로 변조
    observations[0].clock_quality = "unverified"

    context = ActionContext(
        run_id="run-1",
        action_id="act-1",
        window_start_ms=500,
        window_end_ms=3500,
        now_ms=3500,
    )
    goal = Goal(zone_id="right", direction="increase", requested_steps=1)
    verifier = IntentVerifier()
    result = verifier.verify(goal, baseline, observations, context)

    assert result.status == "uncertain"
    assert "clock_unverified" in result.reason_codes
    assert result.goal_satisfied is None
