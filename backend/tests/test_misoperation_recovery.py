"""Tests for Misoperation Recovery Skill and lifecycle (C-YD-03)."""
import asyncio
import pytest
from fastapi.testclient import TestClient

from app.agent.orchestrator import AgentOrchestrator
from app.agent.recovery import MisoperationRecoverySkill, RecoveryDecisionType
from app.api.runs import run_store
from app.contracts.models import Goal, Verdict
from app.main import app
from app.tools.base import ToolRegistry
from app.tools.sensor_tools import AwaitTouchResultTool

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_store():
    run_store.reset()
    yield
    run_store.reset()


def test_recovery_skill_late_success():
    """지연 관측에서 뒤늦게 성공이 포착된 경우 즉시 succeeded 전환 검증."""
    skill = MisoperationRecoverySkill()
    goal = Goal(zone_id="right", direction="increase", requested_steps=1)
    current_verdict = Verdict(
        status="mismatch",
        verification_level="none",
        goal_satisfied=False,
        reason_codes=["no_output_change_detected"],
        evidence_ids=["ev-1"],
    )

    delayed_obs = {
        "field_delta_rms": 45.0,
        "beep_count": 1,
        "status": "valid",
    }

    result = skill.evaluate(
        goal=goal,
        current_verdict=current_verdict,
        retry_count=0,
        delayed_observation=delayed_obs,
    )

    assert result.decision == RecoveryDecisionType.LATE_SUCCESS
    assert result.next_status == "succeeded"
    assert result.updated_verdict is not None
    assert result.updated_verdict.status == "success"
    assert "지연된 신호에서 화력 증가가 확인되었습니다" in result.message


def test_recovery_skill_first_retry_reissues_instruction():
    """1차 미인식 시 새로운 action_id로 재조작 안내 발행 및 awaiting_user 전이 검증."""
    skill = MisoperationRecoverySkill()
    goal = Goal(zone_id="right", direction="increase", requested_steps=1)
    current_verdict = Verdict(
        status="mismatch",
        verification_level="none",
        goal_satisfied=False,
        reason_codes=["no_output_change_detected"],
        evidence_ids=["ev-1"],
    )

    result = skill.evaluate(
        goal=goal,
        current_verdict=current_verdict,
        retry_count=0,
    )

    assert result.decision == RecoveryDecisionType.RETRY_INSTRUCTION
    assert result.next_status == "awaiting_user"
    assert result.instruction is not None
    assert result.instruction.action_id.startswith("act-retry-")
    assert "오른쪽 화구의 + 버튼을 다시 한 번 꾹 눌러 주세요" in result.instruction.text


def test_recovery_skill_second_retry_aborts_to_uncertain():
    """재시도 한도(1회) 소진 시 안전을 위해 uncertain으로 중단 검증."""
    skill = MisoperationRecoverySkill()
    goal = Goal(zone_id="right", direction="increase", requested_steps=1)
    current_verdict = Verdict(
        status="mismatch",
        verification_level="none",
        goal_satisfied=False,
        reason_codes=["no_output_change_detected"],
        evidence_ids=["ev-2"],
    )

    # retry_count = 1 (이미 1회 재시도함)
    result = skill.evaluate(
        goal=goal,
        current_verdict=current_verdict,
        retry_count=1,
    )

    assert result.decision == RecoveryDecisionType.ABORT_UNCERTAIN
    assert result.next_status == "uncertain"
    assert result.updated_verdict is not None
    assert result.updated_verdict.status == "uncertain"
    assert "재조작 후에도 화력 변화를 확인하지 못했습니다" in result.message


def test_recovery_api_full_retry_then_abort_lifecycle():
    """API 종단간 테스트: 1차 미인식 -> 재조작 안내 -> 2차 미인식 -> uncertain 안전 중단."""
    # 변화 없음(0.5 RMS, beep 0)을 반환하는 모의 도구
    registry = ToolRegistry()
    registry.register(
        AwaitTouchResultTool(
            provider=lambda act_id, w: {
                "action_id": act_id,
                "window_ms": w,
                "field_delta_rms": 0.5,
                "beep_count": 0,
                "status": "valid",
            }
        )
    )
    orch = AgentOrchestrator(tool_registry=registry)

    # 1. 초기 실행 생성
    create_res = client.post("/api/runs", json={"user_input": "화력 1단 올려줘", "source": "mock", "zone_id": "right"})
    run_id = create_res.json()["run_id"]
    first_action_id = client.get(f"/api/runs/{run_id}").json()["instruction"]["action_id"]

    # 2. 1차 조작 완료 ack
    client.post(f"/api/runs/{run_id}/ack", json={"action_id": first_action_id})

    # 3. 1차 관측 step -> 변화 없으므로 복구 발동: awaiting_user로 복귀 및 새 instruction 수신
    step1 = asyncio.run(run_store.execute_observation_step(run_id, orchestrator=orch))
    assert step1.status == "awaiting_user"
    assert step1.instruction is not None
    assert step1.instruction.action_id != first_action_id
    second_action_id = step1.instruction.action_id
    assert any(e.kind == "recovery" for e in step1.events)

    # 4. 2차 조작 완료 ack (새 action_id로 ack)
    ack2_res = client.post(f"/api/runs/{run_id}/ack", json={"action_id": second_action_id})
    assert ack2_res.status_code == 200
    assert ack2_res.json()["status"] == "observing"

    # 5. 2차 관측 step -> 또다시 변화 없음 -> 재시도 소진으로 uncertain 안전 중단!
    step2 = asyncio.run(run_store.execute_observation_step(run_id, orchestrator=orch))
    assert step2.status == "uncertain"
    assert step2.verdict is not None
    assert step2.verdict.status == "uncertain"
    assert any("재조작 후에도" in e.message for e in step2.events)

    # 6. 락 해제 확인: 새 실행 생성 가능
    new_res = client.post("/api/runs", json={"user_input": "화력 1단 올려줘", "source": "mock", "zone_id": "left"})
    assert new_res.status_code == 201


def test_recovery_api_retry_then_success_lifecycle():
    """API 종단간 테스트: 1차 미인식 -> 재조작 안내 -> 2차에서 성공(succeeded)."""
    call_count = 0

    def mock_touch_provider(act_id: str, w: int):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # 1차: 터치 미인식
            return {"action_id": act_id, "window_ms": w, "field_delta_rms": 0.5, "beep_count": 0, "status": "valid"}
        else:
            # 2차: 정상 터치 성공
            return {"action_id": act_id, "window_ms": w, "field_delta_rms": 45.0, "beep_count": 1, "status": "valid"}

    registry = ToolRegistry()
    registry.register(AwaitTouchResultTool(provider=mock_touch_provider))
    orch = AgentOrchestrator(tool_registry=registry)

    create_res = client.post("/api/runs", json={"user_input": "화력 1단 올려줘", "source": "mock", "zone_id": "right"})
    run_id = create_res.json()["run_id"]
    action_1 = client.get(f"/api/runs/{run_id}").json()["instruction"]["action_id"]

    # 1차 ack & step -> 복구 유도
    client.post(f"/api/runs/{run_id}/ack", json={"action_id": action_1})
    step1 = asyncio.run(run_store.execute_observation_step(run_id, orchestrator=orch))
    assert step1.status == "awaiting_user"
    action_2 = step1.instruction.action_id

    # 2차 ack & step -> 이번엔 성공!
    client.post(f"/api/runs/{run_id}/ack", json={"action_id": action_2})
    step2 = asyncio.run(run_store.execute_observation_step(run_id, orchestrator=orch))
    assert step2.status == "succeeded"
    assert step2.verdict.status == "success"
    assert any("화력이 한 단계 올라갔습니다" in e.message for e in step2.events)
