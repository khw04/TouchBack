"""Tests for Misoperation Recovery Skill and lifecycle (C-YD-03)."""
import asyncio
import pytest
from fastapi.testclient import TestClient

from app.adapters.mock import MockAdapter, ScheduledObservation, VirtualClock
from app.adapters.mock_setup import build_mock_adapter
from app.agent.orchestrator import AgentOrchestrator
from app.agent.recovery import MisoperationRecoverySkill, RecoveryDecisionType
from app.agent.verifier import IntentVerifier
from app.api.runs import run_store
from app.contracts.models import DeviceObservation, FieldSummary, Goal, Quality, Verdict
from app.main import app

client = TestClient(app)


def build_multi_step_mock_adapter(*, second_step_success: bool = True) -> MockAdapter:
    """동일한 어댑터와 가상 클럭으로 1차 조작(변화없음)과 2차 재시도(신선한 기준선+2차 관측)를 연속 지원하는 Mock 어댑터."""
    def frame(seq: int, start: int, end: int, arrival: int, rms: float, quality: Quality = Quality(status="valid", reasons=[])):
        device = DeviceObservation(
            schema_version="0.2", device_id="mock-device-01", session_id="mock-session-01",
            seq=seq, source="mock", zone_id="right", window_start_ms=start, window_end_ms=end,
            beep_count=1 if rms > 105.0 else 0,
            field_summary=FieldSummary(rms=rms, unit="adc_count", sample_count=end-start, sample_rate_hz=1000, method="ac_rms_v1"),
            quality=quality, calibration_id="mock-test-v1",
        )
        return ScheduledObservation(arrival, device)

    schedule = [
        # 1차 기준선 (seq 0~4, 0~500ms)
        frame(0, 0, 100, 100, 100.0),
        frame(1, 100, 200, 200, 100.0),
        frame(2, 200, 300, 300, 100.0),
        frame(3, 300, 400, 400, 100.0),
        frame(4, 400, 500, 500, 100.0),
        # 1차 조작 관측: 변화 없음 (seq 5, 1000~1500ms, arrival 1500ms, RMS 100.0)
        frame(5, 1000, 1500, 1500, 100.0),
        # 2차 조작을 위한 신선한 기준선: (seq 6, 4000~4500ms, arrival 4500ms, RMS 100.0)
        frame(6, 4000, 4500, 4500, 100.0),
        # 2차 조작 관측: (seq 7, 5000~5500ms, arrival 5500ms, RMS 120.0 or 100.0)
        frame(7, 5000, 5500, 5500, 120.0 if second_step_success else 100.0),
    ]
    return MockAdapter(schedule, VirtualClock(now_ms=500))


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


def test_recovery_skill_aborts_when_delayed_observation_shows_output_change():
    """리뷰어 지적사항 반영: 지연 관측에서 output_change 감지 시 최초 no_output_change 무효화 및 재조작 금지."""
    skill = MisoperationRecoverySkill()
    goal = Goal(zone_id="right", direction="increase", requested_steps=1)
    current_verdict = Verdict(
        status="mismatch",
        verification_level="none",
        goal_satisfied=False,
        reason_codes=["no_output_change"],
        evidence_ids=["ev-1"],
    )

    # 지연 관측에서 출력 증가 감지 (uncertain / output_change)
    delayed_verdict = Verdict(
        status="uncertain",
        verification_level="output_change",
        goal_satisfied=None,
        reason_codes=["step_unverified"],
        evidence_ids=["ev-delayed-1"],
    )

    result = skill.evaluate(
        goal=goal,
        current_verdict=current_verdict,
        retry_count=0,
        delayed_verdict=delayed_verdict,
    )

    assert result.decision == RecoveryDecisionType.ABORT_UNCERTAIN
    assert result.next_status == "uncertain"
    assert result.instruction is None
    assert "출력 변화 또는 판정 불가가 확인되었습니다" in result.message


def test_recovery_skill_aborts_when_delayed_observation_has_sensor_error():
    """리뷰어 지적사항 반영: 지연 관측에서 센서 품질 이상 감지 시 재조작 금지 및 안전 중단."""
    skill = MisoperationRecoverySkill()
    goal = Goal(zone_id="right", direction="increase", requested_steps=1)
    current_verdict = Verdict(
        status="mismatch",
        verification_level="none",
        goal_satisfied=False,
        reason_codes=["no_output_change"],
        evidence_ids=["ev-1"],
    )

    delayed_verdict = Verdict(
        status="uncertain",
        verification_level="none",
        goal_satisfied=None,
        reason_codes=["sensor_quality_invalid"],
        evidence_ids=["ev-delayed-faulty"],
    )

    result = skill.evaluate(
        goal=goal,
        current_verdict=current_verdict,
        retry_count=0,
        delayed_verdict=delayed_verdict,
    )

    assert result.decision == RecoveryDecisionType.ABORT_UNCERTAIN
    assert result.next_status == "uncertain"
    assert result.instruction is None


def test_recovery_skill_aborts_when_new_baseline_is_stale():
    """리뷰어 지적사항 반영: 새 기준선이 만료(stale_baseline)되었을 때 재조작 안내 금지 및 안전 중단."""
    skill = MisoperationRecoverySkill()
    verifier = IntentVerifier()
    goal = Goal(zone_id="right", direction="increase", requested_steps=1)
    current_verdict = Verdict(
        status="mismatch",
        verification_level="none",
        goal_satisfied=False,
        reason_codes=["no_output_change"],
        evidence_ids=["ev-1"],
    )

    adapter = build_mock_adapter("no_change", zone_id="right")
    # seq 5: end=1500ms
    baseline = adapter.read_baseline("right")

    # now_ms = 4500ms (age = 4500 - 1500 = 3000ms > max_baseline_age_ms 500ms)
    result = skill.evaluate(
        goal=goal,
        current_verdict=current_verdict,
        retry_count=0,
        new_baseline=baseline,
        now_ms=4500,
        calibration=verifier.calibration,
        sensor_integrity_ok=True,
        delayed_checked=True,
        next_window_start_ms=4500,
        next_window_end_ms=7500,
    )

    assert result.decision == RecoveryDecisionType.ABORT_UNCERTAIN
    assert result.next_status == "uncertain"
    assert result.updated_verdict is not None
    assert "baseline_stale" in result.updated_verdict.reason_codes
    assert "너무 오래되었습니다" in result.message


def test_recovery_skill_first_retry_reissues_instruction_with_fresh_baseline():
    """신선한 새 기준선(age <= 500ms)이 확인된 경우에만 분리된 새 시간창과 새 action_id 발급."""
    skill = MisoperationRecoverySkill()
    verifier = IntentVerifier()
    goal = Goal(zone_id="right", direction="increase", requested_steps=1)
    current_verdict = Verdict(
        status="mismatch",
        verification_level="none",
        goal_satisfied=False,
        reason_codes=["no_output_change"],
        evidence_ids=["ev-1"],
    )

    # 4000~4500ms 기준선 생성
    adapter = build_multi_step_mock_adapter()
    # clock을 4500ms로 진행시켜 seq 6을 수신
    adapter.await_touch_result(run_id="run-1", action_id="act-1", zone_id="right", window_start_ms=500, window_end_ms=3500, timeout_ms=4000)
    fresh_baseline = adapter.read_baseline("right")
    assert fresh_baseline.mapped_window_end_ms == 4500

    result = skill.evaluate(
        goal=goal,
        current_verdict=current_verdict,
        retry_count=0,
        new_baseline=fresh_baseline,
        now_ms=4500,
        calibration=verifier.calibration,
        sensor_integrity_ok=True,
        delayed_checked=True,
        next_window_start_ms=4500,
        next_window_end_ms=7500,
    )

    assert result.decision == RecoveryDecisionType.RETRY_INSTRUCTION
    assert result.next_status == "awaiting_user"
    assert result.instruction is not None
    assert result.instruction.action_id.startswith("act-retry-")
    assert result.next_window_start_ms == 4500
    assert result.next_window_end_ms == 7500
    assert "오른쪽 화구의 + 버튼을 다시 한 번 꾹 눌러 주세요" in result.instruction.text


def test_recovery_skill_second_retry_aborts_to_uncertain():
    """재시도 한도(1회) 소진 시 안전을 위해 uncertain으로 중단 검증."""
    skill = MisoperationRecoverySkill()
    verifier = IntentVerifier()
    goal = Goal(zone_id="right", direction="increase", requested_steps=1)
    current_verdict = Verdict(
        status="mismatch",
        verification_level="none",
        goal_satisfied=False,
        reason_codes=["no_output_change"],
        evidence_ids=["ev-2"],
    )

    adapter = build_multi_step_mock_adapter()
    adapter.await_touch_result(run_id="run-1", action_id="act-1", zone_id="right", window_start_ms=500, window_end_ms=3500, timeout_ms=4000)
    fresh_baseline = adapter.read_baseline("right")

    # retry_count = 1 (이미 1회 재시도함)
    result = skill.evaluate(
        goal=goal,
        current_verdict=current_verdict,
        retry_count=1,
        new_baseline=fresh_baseline,
        now_ms=4500,
        calibration=verifier.calibration,
        sensor_integrity_ok=True,
        delayed_checked=True,
        next_window_start_ms=4500,
        next_window_end_ms=7500,
    )

    assert result.decision == RecoveryDecisionType.ABORT_UNCERTAIN
    assert result.next_status == "uncertain"
    assert result.updated_verdict is not None
    assert result.updated_verdict.status == "uncertain"
    assert "재조작 후에도 화력 변화를 확인하지 못했습니다" in result.message


def test_recovery_api_aborts_on_unrefreshed_stale_baseline():
    """리뷰어 지적사항 검증: 기본 no_change 어댑터는 지연 후 기준선이 만료(stale)되므로 안전하게 uncertain 종료."""
    adapter = build_mock_adapter("no_change", zone_id="right")
    orch = AgentOrchestrator(adapter=adapter)

    create_res = client.post("/api/runs", json={"user_input": "화력 1단 올려줘", "source": "mock", "zone_id": "right"})
    run_id = create_res.json()["run_id"]
    action_id = client.get(f"/api/runs/{run_id}").json()["instruction"]["action_id"]

    client.post(f"/api/runs/{run_id}/ack", json={"action_id": action_id})

    # 1차 관측 step -> 신선한 새 기준선이 없으므로(1500ms vs 4500ms) uncertain으로 안전 중단
    step = asyncio.run(run_store.execute_observation_step(run_id, orchestrator=orch))
    assert step.status == "uncertain"
    assert step.verdict is not None
    assert "baseline_stale" in step.verdict.reason_codes


def test_recovery_api_same_adapter_multi_step_retry_lifecycle():
    """리뷰어 지적사항 검증: 동일한 adapter 및 가상 클럭을 유지한 채 1차 미인식 -> 재조작 안내 -> 2차 성공 완주."""
    # 동일한 어댑터 인스턴스 1개만 생성하여 전체 라이프사이클에 유지
    multi_adapter = build_multi_step_mock_adapter(second_step_success=True)
    orch = AgentOrchestrator(adapter=multi_adapter)

    # 1. 실행 생성
    create_res = client.post("/api/runs", json={"user_input": "화력 1단 올려줘", "source": "mock", "zone_id": "right"})
    run_id = create_res.json()["run_id"]
    first_action_id = client.get(f"/api/runs/{run_id}").json()["instruction"]["action_id"]

    # 2. 1차 조작 완료 ack (500~3500ms 시간창)
    client.post(f"/api/runs/{run_id}/ack", json={"action_id": first_action_id})

    # 3. 1차 관측 step -> seq 5 (변화 없음) -> 지연 확인 후 seq 6(신선한 기준선) 검증 통과 -> awaiting_user 복귀 및 새 시간창(4500~7500ms) 발급
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

    # 5. 2차 관측 step -> 동일한 어댑터의 4500~7500ms 시간창에서 seq 7(RMS 120.0) 관측 -> 1단 증가 성공!
    step2 = asyncio.run(run_store.execute_observation_step(run_id, orchestrator=orch))
    assert step2.status == "succeeded"
    assert step2.verdict is not None
    assert step2.verdict.status == "success"
    assert step2.verdict.verification_level == "exact_step"
    assert any("화력이 한 단계 올라갔습니다" in e.message for e in step2.events)

    # 6. 락 정상 해제 확인: 새 실행 생성 가능
    new_res = client.post("/api/runs", json={"user_input": "화력 1단 올려줘", "source": "mock", "zone_id": "left"})
    assert new_res.status_code == 201


def test_atomic_cancellation_during_observation_prevents_dual_awaiting_user():
    """리뷰어 지적사항 3 검증: 관측 await 중 cancel_run 호출 시 stopped 유지 및 타 run과의 awaiting_user 경합 방지."""
    adapter = build_multi_step_mock_adapter()

    class CancellingOrchestrator(AgentOrchestrator):
        async def execute_observation_and_verification(self, goal, action_id, run_id="run-default", **kwargs):
            run_store.cancel_run(run_id)
            return await super().execute_observation_and_verification(goal, action_id, run_id=run_id, **kwargs)

    cancelling_orch = CancellingOrchestrator(adapter=adapter)

    create1 = client.post("/api/runs", json={"user_input": "화력 1단 올려줘", "source": "mock", "zone_id": "right"})
    run1_id = create1.json()["run_id"]
    action1_id = client.get(f"/api/runs/{run1_id}").json()["instruction"]["action_id"]
    client.post(f"/api/runs/{run1_id}/ack", json={"action_id": action1_id})

    step1_res = asyncio.run(run_store.execute_observation_step(run1_id, orchestrator=cancelling_orch))
    assert step1_res.status == "stopped"

    get1 = client.get(f"/api/runs/{run1_id}").json()
    assert get1["status"] == "stopped"

    create2 = client.post("/api/runs", json={"user_input": "화력 1단 올려줘", "source": "mock", "zone_id": "right"})
    assert create2.status_code == 201
    run2_id = create2.json()["run_id"]
    get2 = client.get(f"/api/runs/{run2_id}").json()
    assert get2["status"] == "awaiting_user"

    assert client.get(f"/api/runs/{run1_id}").json()["status"] == "stopped"
