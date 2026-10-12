"""Tests for Misoperation Recovery Skill and lifecycle (C-YD-03)."""
import asyncio
import pytest
from fastapi.testclient import TestClient

from app.adapters.mock_setup import build_mock_adapter
from app.agent.orchestrator import AgentOrchestrator
from app.agent.recovery import MisoperationRecoverySkill, RecoveryDecisionType
from app.api.runs import run_store
from app.contracts.models import Goal, Verdict
from app.main import app

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
        reason_codes=["no_output_change"],
        evidence_ids=["ev-1"],
    )

    late_verdict = Verdict(
        status="success",
        verification_level="exact_step",
        goal_satisfied=True,
        reason_codes=["requested_step_verified"],
        evidence_ids=["ev-1", "ev-late"],
    )

    result = skill.evaluate(
        goal=goal,
        current_verdict=current_verdict,
        retry_count=0,
        delayed_verdict=late_verdict,
    )

    assert result.decision == RecoveryDecisionType.LATE_SUCCESS
    assert result.next_status == "succeeded"
    assert result.updated_verdict is not None
    assert result.updated_verdict.status == "success"
    assert "지연된 신호에서 화력 증가가 확인되었습니다" in result.message


def test_recovery_skill_first_retry_reissues_instruction():
    """1차 미인식(유효한 변화 없음) 시 새 기준선 확보 후 새 action_id로 재조작 안내 검증."""
    skill = MisoperationRecoverySkill()
    goal = Goal(zone_id="right", direction="increase", requested_steps=1)
    current_verdict = Verdict(
        status="mismatch",
        verification_level="none",
        goal_satisfied=False,
        reason_codes=["no_output_change"],
        evidence_ids=["ev-1"],
    )

    adapter = build_mock_adapter("no_change", zone_id="right")
    baseline = adapter.read_baseline("right")

    result = skill.evaluate(
        goal=goal,
        current_verdict=current_verdict,
        retry_count=0,
        new_baseline=baseline,
        sensor_integrity_ok=True,
        delayed_checked=True,
    )

    assert result.decision == RecoveryDecisionType.RETRY_INSTRUCTION
    assert result.next_status == "awaiting_user"
    assert result.instruction is not None
    assert result.instruction.action_id.startswith("act-retry-")
    assert "오른쪽 화구의 + 버튼을 다시 한 번 꾹 눌러 주세요" in result.instruction.text


def test_recovery_skill_aborts_on_missing_or_unverified_delayed():
    """리뷰어 지적사항 2/4 검증: 관측 누락 또는 지연 관측 미확인 시 재조작 없이 uncertain 중단."""
    skill = MisoperationRecoverySkill()
    goal = Goal(zone_id="right", direction="increase", requested_steps=1)

    # 1. 누락 관측 (observations_missing)
    missing_verdict = Verdict(
        status="uncertain",
        verification_level="none",
        goal_satisfied=None,
        reason_codes=["observations_missing"],
        evidence_ids=[],
    )
    res_missing = skill.evaluate(goal, missing_verdict, retry_count=0)
    assert res_missing.decision == RecoveryDecisionType.ABORT_UNCERTAIN
    assert res_missing.next_status == "uncertain"
    assert "조작 결과를 신뢰할 수 없습니다" in res_missing.message

    # 2. 변화 없음이지만 지연 검사를 확인할 수 없음 (delayed_checked=False)
    no_change_verdict = Verdict(
        status="mismatch",
        verification_level="none",
        goal_satisfied=False,
        reason_codes=["no_output_change"],
        evidence_ids=["ev-1"],
    )
    res_delayed_unverified = skill.evaluate(
        goal, no_change_verdict, retry_count=0, delayed_checked=False
    )
    assert res_delayed_unverified.decision == RecoveryDecisionType.ABORT_UNCERTAIN
    assert res_delayed_unverified.next_status == "uncertain"
    assert "지연 관측을 확인할 수 없습니다" in res_delayed_unverified.message

    # 3. 새 기준선 확보 불가 (new_baseline=None)
    res_no_base = skill.evaluate(
        goal, no_change_verdict, retry_count=0, delayed_checked=True, new_baseline=None
    )
    assert res_no_base.decision == RecoveryDecisionType.ABORT_UNCERTAIN
    assert res_no_base.next_status == "uncertain"
    assert "새 조작을 위한 기준 신호를 확보하지 못했습니다" in res_no_base.message


def test_recovery_skill_second_retry_aborts_to_uncertain():
    """재시도 한도(1회) 소진 시 안전을 위해 uncertain으로 중단 검증."""
    skill = MisoperationRecoverySkill()
    goal = Goal(zone_id="right", direction="increase", requested_steps=1)
    current_verdict = Verdict(
        status="mismatch",
        verification_level="none",
        goal_satisfied=False,
        reason_codes=["no_output_change"],
        evidence_ids=["ev-2"],
    )
    adapter = build_mock_adapter("no_change", zone_id="right")
    baseline = adapter.read_baseline("right")

    # retry_count = 1 (이미 1회 재시도함)
    result = skill.evaluate(
        goal=goal,
        current_verdict=current_verdict,
        retry_count=1,
        new_baseline=baseline,
        sensor_integrity_ok=True,
        delayed_checked=True,
    )

    assert result.decision == RecoveryDecisionType.ABORT_UNCERTAIN
    assert result.next_status == "uncertain"
    assert result.updated_verdict is not None
    assert result.updated_verdict.status == "uncertain"
    assert "재조작 후에도 화력 변화를 확인하지 못했습니다" in result.message


def test_recovery_api_delayed_success_lifecycle():
    """리뷰어 지적사항 4 검증: delayed 시나리오에서 execute_observation_step 실행 시 late_success로 succeeded 전이."""
    adapter = build_mock_adapter("delayed", zone_id="right")
    orch = AgentOrchestrator(adapter=adapter)

    create_res = client.post("/api/runs", json={"user_input": "화력 1단 올려줘", "source": "mock", "zone_id": "right"})
    run_id = create_res.json()["run_id"]
    action_id = client.get(f"/api/runs/{run_id}").json()["instruction"]["action_id"]

    client.post(f"/api/runs/{run_id}/ack", json={"action_id": action_id})

    # execute_observation_step 실행
    res = asyncio.run(run_store.execute_observation_step(run_id, orchestrator=orch))

    # 지연 도착 프레임이 수집되어 성공으로 전이
    assert res.status == "succeeded"
    assert res.verdict is not None
    assert res.verdict.status == "success"
    assert res.verdict.verification_level == "exact_step"
    assert any("화력이 한 단계 올라갔습니다" in e.message for e in res.events)


def test_recovery_api_full_retry_then_abort_lifecycle():
    """API 종단간 테스트: 1차 미인식 -> 재조작 안내 -> 2차 미인식 -> uncertain 안전 중단."""
    adapter = build_mock_adapter("no_change", zone_id="right")
    orch = AgentOrchestrator(adapter=adapter)

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
    adapter2 = build_mock_adapter("no_change", zone_id="right")
    orch2 = AgentOrchestrator(adapter=adapter2)
    step2 = asyncio.run(run_store.execute_observation_step(run_id, orchestrator=orch2))
    assert step2.status == "uncertain"
    assert step2.verdict is not None
    assert step2.verdict.status == "uncertain"
    assert any("재조작 후에도" in e.message for e in step2.events)

    # 6. 락 해제 확인: 새 실행 생성 가능
    new_res = client.post("/api/runs", json={"user_input": "화력 1단 올려줘", "source": "mock", "zone_id": "left"})
    assert new_res.status_code == 201


def test_atomic_cancellation_during_observation_prevents_dual_awaiting_user():
    """리뷰어 지적사항 3 검증: 관측 await 중 cancel_run 호출 시 stopped 유지 및 타 run과의 awaiting_user 경합 방지."""
    adapter = build_mock_adapter("no_change", zone_id="right")

    # 관측 실행 중 cancel_run을 호출하는 가짜 오케스트레이터 시뮬레이션
    class CancellingOrchestrator(AgentOrchestrator):
        async def execute_observation_and_verification(self, goal, action_id, run_id="run-default", **kwargs):
            # 관측 도중 클라이언트가 cancel_run을 호출
            run_store.cancel_run(run_id)
            # 늦은 결과 반환
            return await super().execute_observation_and_verification(goal, action_id, run_id=run_id, **kwargs)

    cancelling_orch = CancellingOrchestrator(adapter=adapter)

    # 1. Run 1 생성 및 ack
    create1 = client.post("/api/runs", json={"user_input": "화력 1단 올려줘", "source": "mock", "zone_id": "right"})
    run1_id = create1.json()["run_id"]
    action1_id = client.get(f"/api/runs/{run1_id}").json()["instruction"]["action_id"]
    client.post(f"/api/runs/{run1_id}/ack", json={"action_id": action1_id})

    # 2. Step 실행 중 취소 발생 -> 결과는 stopped로 유지되어야 함
    step1_res = asyncio.run(run_store.execute_observation_step(run1_id, orchestrator=cancelling_orch))
    assert step1_res.status == "stopped"

    # 3. run1의 최종 상태가 awaiting_user로 덮어써지지 않았는지 확인
    get1 = client.get(f"/api/runs/{run1_id}").json()
    assert get1["status"] == "stopped"

    # 4. 이제 새 Run 2 생성 -> 락이 정상 확보되고 Run 2만 awaiting_user 상태를 가짐
    create2 = client.post("/api/runs", json={"user_input": "화력 1단 올려줘", "source": "mock", "zone_id": "right"})
    assert create2.status_code == 201
    run2_id = create2.json()["run_id"]
    get2 = client.get(f"/api/runs/{run2_id}").json()
    assert get2["status"] == "awaiting_user"

    # run1은 여전히 stopped 상태 유지 (동시 awaiting_user 버그 방지 확인)
    assert client.get(f"/api/runs/{run1_id}").json()["status"] == "stopped"
