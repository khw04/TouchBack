"""Tests for Execution API (C-YD-00): run lifecycle, concurrency lock, timeouts, ack, and cancel."""
from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient

from app.api.runs import run_store
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_store():
    run_store.reset()
    yield
    run_store.reset()


def test_create_run_with_zone():
    payload = {"user_input": "화력 1단 올려줘", "source": "mock", "zone_id": "right"}
    response = client.post("/api/runs", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["run_id"].startswith("run-")
    assert data["status"] == "awaiting_user"
    assert data["execution_mode"] == "backend_stub"

    # Verify run details
    get_res = client.get(f"/api/runs/{data['run_id']}")
    assert get_res.status_code == 200
    run_data = get_res.json()
    assert run_data["source"] == "mock"
    assert run_data["instruction"] is not None
    assert run_data["instruction"]["text"] == "오른쪽 화구의 + 버튼을 한 번 눌러 주세요."
    assert len(run_data["events"]) >= 2
    assert any(e["kind"] == "instruction" for e in run_data["events"])


def test_create_run_without_zone():
    payload = {"user_input": "화력 1단 올려줘", "source": "mock", "zone_id": None}
    response = client.post("/api/runs", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "awaiting_clarification"

    get_res = client.get(f"/api/runs/{data['run_id']}")
    assert get_res.status_code == 200
    run_data = get_res.json()
    assert run_data["instruction"] is None
    assert any("화구가 지정되지 않았습니다" in e["message"] for e in run_data["events"])


def test_reject_live_source_when_unprepared():
    """P1 피드백: live 센서 연동 미준비 상태에서는 422 validation 오류로 명시적 거부."""
    payload = {"user_input": "화력 1단 올려줘", "source": "live", "zone_id": "right"}
    response = client.post("/api/runs", json=payload)
    assert response.status_code == 422
    err = response.json()
    assert err["code"] == "validation"
    assert "live 센서 연동이 준비되지 않았습니다" in err["message"]


def test_unsupported_goal_does_not_issue_instruction():
    """P1 피드백: '인덕션 꺼줘' 등 미지원 목표 입력 시 + 조작 안내를 게시하지 않고 명확화 처리."""
    payload = {"user_input": "인덕션 꺼줘", "source": "mock", "zone_id": "right"}
    response = client.post("/api/runs", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "awaiting_clarification"

    get_res = client.get(f"/api/runs/{data['run_id']}")
    assert get_res.status_code == 200
    run_data = get_res.json()
    # 사용자 목표와 무관한 조작 안내(Instruction)는 절대 나가지 않아야 함
    assert run_data["instruction"] is None
    assert any("지원하지 않거나 준비되지 않은 목표" in e["message"] for e in run_data["events"])


def test_single_active_run_concurrency_lock():
    payload1 = {"user_input": "화력 올려줘", "source": "mock", "zone_id": "right"}
    res1 = client.post("/api/runs", json=payload1)
    assert res1.status_code == 201

    payload2 = {"user_input": "화력 1단 올려줘", "source": "mock", "zone_id": "left"}
    res2 = client.post("/api/runs", json=payload2)
    assert res2.status_code == 409
    err = res2.json()
    assert err["code"] == "busy"
    assert "현재 진행 중인 다른 실행이 있습니다" in err["message"]


def test_get_nonexistent_run():
    response = client.get("/api/runs/non-existent-id")
    assert response.status_code == 404
    err = response.json()
    assert err["code"] == "unknown_run"


def test_ack_lifecycle_and_idempotency():
    # 1. Start a run with right zone
    create_res = client.post("/api/runs", json={"user_input": "화력 1단 올려줘", "source": "mock", "zone_id": "right"})
    run_id = create_res.json()["run_id"]
    run_info = client.get(f"/api/runs/{run_id}").json()
    action_id = run_info["instruction"]["action_id"]

    # 2. Ack with correct action_id
    ack_res = client.post(f"/api/runs/{run_id}/ack", json={"action_id": action_id})
    assert ack_res.status_code == 200
    ack_data = ack_res.json()
    assert ack_data["accepted"] is True
    assert ack_data["status"] == "observing"

    # 3. Repeated ack with same action_id is idempotent
    ack_res2 = client.post(f"/api/runs/{run_id}/ack", json={"action_id": action_id})
    assert ack_res2.status_code == 200
    assert ack_res2.json()["accepted"] is True

    # 4. Unknown or stale action_id should return 409 stale_action
    stale_res = client.post(f"/api/runs/{run_id}/ack", json={"action_id": "act-invalid"})
    assert stale_res.status_code == 409
    assert stale_res.json()["code"] == "stale_action"


def test_cancel_lifecycle_and_unlock():
    create_res = client.post("/api/runs", json={"user_input": "화력 올려줘", "source": "mock", "zone_id": "right"})
    run_id = create_res.json()["run_id"]

    # Cancel the active run
    cancel_res = client.post(f"/api/runs/{run_id}/cancel")
    assert cancel_res.status_code == 200
    assert cancel_res.json()["status"] == "stopped"

    # Verify run status is stopped and cancellation event logged
    get_res = client.get(f"/api/runs/{run_id}").json()
    assert get_res["status"] == "stopped"
    assert any("실행이 취소되었습니다" in e["message"] for e in get_res["events"])

    # Idempotent cancel
    cancel_res2 = client.post(f"/api/runs/{run_id}/cancel")
    assert cancel_res2.status_code == 200
    assert cancel_res2.json()["status"] == "stopped"

    # Lock is released: a new run can now be created
    create_res2 = client.post("/api/runs", json={"user_input": "화력 1단 올려줘", "source": "mock", "zone_id": "left"})
    assert create_res2.status_code == 201


def test_ack_on_terminal_run_returns_409():
    create_res = client.post("/api/runs", json={"user_input": "화력 올려줘", "source": "mock", "zone_id": "right"})
    run_id = create_res.json()["run_id"]
    client.post(f"/api/runs/{run_id}/cancel")

    # Now run is stopped (terminal)
    ack_res = client.post(f"/api/runs/{run_id}/ack", json={"action_id": "act-any"})
    assert ack_res.status_code == 409
    assert ack_res.json()["code"] == "terminal_run"


def test_timeout_awaiting_user_and_unlock_with_virtual_clock():
    """P2 피드백: awaiting_user 60초 미응답 종료, 활성 락 해제, 늦은 ack 409 검증."""
    t0 = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)
    current_time = t0
    run_store.set_clock(lambda: current_time)

    # 1. 생성 시점 (t0)
    create_res = client.post("/api/runs", json={"user_input": "화력 1단 올려줘", "source": "mock", "zone_id": "right"})
    assert create_res.status_code == 201
    run_id = create_res.json()["run_id"]
    action_id = client.get(f"/api/runs/{run_id}").json()["instruction"]["action_id"]

    # 2. 59초 경과 (경계값: 만료 전)
    current_time = t0 + timedelta(seconds=59)
    run_state = client.get(f"/api/runs/{run_id}").json()
    assert run_state["status"] == "awaiting_user"

    # 다른 실행 시도 시 busy 충돌 유지
    busy_res = client.post("/api/runs", json={"user_input": "화력 1단 올려줘", "source": "mock", "zone_id": "left"})
    assert busy_res.status_code == 409
    assert busy_res.json()["code"] == "busy"

    # 3. 60초 경과 (만료 시점)
    current_time = t0 + timedelta(seconds=60)
    expired_state = client.get(f"/api/runs/{run_id}").json()
    assert expired_state["status"] == "stopped"
    assert any("대기 시간(60초) 초과" in e["message"] for e in expired_state["events"])

    # 4. 늦은 ack 시도 -> terminal_run (409)
    late_ack = client.post(f"/api/runs/{run_id}/ack", json={"action_id": action_id})
    assert late_ack.status_code == 409
    assert late_ack.json()["code"] == "terminal_run"

    # 5. 활성 락 해제 확인: 새 실행 생성 가능
    new_run_res = client.post("/api/runs", json={"user_input": "화력 1단 올려줘", "source": "mock", "zone_id": "left"})
    assert new_run_res.status_code == 201


def test_timeout_awaiting_clarification_and_unlock_with_virtual_clock():
    """P2 피드백: awaiting_clarification 120초 미응답 종료 및 활성 락 해제 검증."""
    t0 = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)
    current_time = t0
    run_store.set_clock(lambda: current_time)

    # 1. 화구 미지정으로 생성 -> awaiting_clarification
    create_res = client.post("/api/runs", json={"user_input": "화력 1단 올려줘", "source": "mock", "zone_id": None})
    assert create_res.status_code == 201
    run_id = create_res.json()["run_id"]

    # 2. 119초 경과 (만료 전)
    current_time = t0 + timedelta(seconds=119)
    run_state = client.get(f"/api/runs/{run_id}").json()
    assert run_state["status"] == "awaiting_clarification"

    busy_res = client.post("/api/runs", json={"user_input": "화력 1단 올려줘", "source": "mock", "zone_id": "left"})
    assert busy_res.status_code == 409

    # 3. 120초 경과 -> stopped 및 락 해제
    current_time = t0 + timedelta(seconds=120)
    expired_state = client.get(f"/api/runs/{run_id}").json()
    assert expired_state["status"] == "stopped"
    assert any("대기 시간(120초) 초과" in e["message"] for e in expired_state["events"])

    # 4. 새 실행 생성 허용
    new_run_res = client.post("/api/runs", json={"user_input": "화력 1단 올려줘", "source": "mock", "zone_id": "right"})
    assert new_run_res.status_code == 201


def test_atomic_expiration_on_create_run():
    """만료된 실행이 있는 상태에서 get_run을 거치지 않고 바로 새 create_run이 호출되어도 원자적으로 만료 처리 후 생성 허용."""
    t0 = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)
    current_time = t0
    run_store.set_clock(lambda: current_time)

    res1 = client.post("/api/runs", json={"user_input": "화력 1단 올려줘", "source": "mock", "zone_id": "right"})
    assert res1.status_code == 201
    old_run_id = res1.json()["run_id"]

    # 65초 경과 후 직접 create_run 호출
    current_time = t0 + timedelta(seconds=65)
    res2 = client.post("/api/runs", json={"user_input": "화력 1단 올려줘", "source": "mock", "zone_id": "left"})
    assert res2.status_code == 201

    # 이전 실행은 자동으로 stopped 되었는지 확인
    old_state = client.get(f"/api/runs/{old_run_id}").json()
    assert old_state["status"] == "stopped"


def test_validation_error_format():
    # Missing required field user_input
    response = client.post("/api/runs", json={"source": "mock"})
    assert response.status_code == 422
    err = response.json()
    assert err["code"] == "validation"
    assert "요청 형식" in err["message"]


@pytest.mark.parametrize("user_input", ["화력 올려주지 마", "화력 1단 증가시키지 마", "화력 안 올려줘", "화력 2단 올려줘", "화력 두 단계 올려줘"])
def test_negated_or_out_of_scope_goal_requires_clarification(user_input):
    response = client.post("/api/runs", json={"user_input": user_input, "source": "mock", "zone_id": "right"})
    assert response.status_code == 201
    result = client.get("/api/runs/" + response.json()["run_id"]).json()
    assert result["status"] == "awaiting_clarification"
    assert result["instruction"] is None


def test_run_step_unconfigured_sensor_returns_uncertain():
    """기본 미설정 센서 환경에서 step 호출 시 안전하게 uncertain 판정 및 락 해제 (C-YD-02)."""
    create_res = client.post("/api/runs", json={"user_input": "화력 1단 올려줘", "source": "mock", "zone_id": "right"})
    assert create_res.status_code == 201
    run_id = create_res.json()["run_id"]
    action_id = client.get(f"/api/runs/{run_id}").json()["instruction"]["action_id"]

    # 1. ack -> observing
    ack_res = client.post(f"/api/runs/{run_id}/ack", json={"action_id": action_id})
    assert ack_res.status_code == 200
    assert ack_res.json()["status"] == "observing"

    # 2. step -> 미설정 센서이므로 uncertain 종료
    step_res = client.post(f"/api/runs/{run_id}/step")
    assert step_res.status_code == 200
    step_data = step_res.json()
    assert step_data["status"] == "uncertain"
    assert step_data["verdict"] is not None
    assert step_data["verdict"]["status"] == "uncertain"
    assert any("결과를 확인할 수 없습니다" in e["message"] for e in step_data["events"])

    # 3. 락 해제 확인: 새 실행 생성 가능
    new_res = client.post("/api/runs", json={"user_input": "화력 1단 올려줘", "source": "mock", "zone_id": "left"})
    assert new_res.status_code == 201


def test_run_step_with_mock_orchestrator_returns_succeeded():
    """Mock 센서 어댑터가 연결된 오케스트레이터로 step 실행 시 정상 1단계 성공 (C-YD-02)."""
    import asyncio
    from app.agent.orchestrator import AgentOrchestrator

    create_res = client.post("/api/runs", json={"user_input": "화력 1단 올려줘", "source": "mock", "zone_id": "right"})
    run_id = create_res.json()["run_id"]
    action_id = client.get(f"/api/runs/{run_id}").json()["instruction"]["action_id"]

    client.post(f"/api/runs/{run_id}/ack", json={"action_id": action_id})

    # Mock 어댑터 오케스트레이터 주입
    mock_orch = AgentOrchestrator()
    res = asyncio.run(run_store.execute_observation_step(run_id, orchestrator=mock_orch))

    assert res.status == "succeeded"
    assert res.verdict is not None
    assert res.verdict.status == "success"
    assert res.verdict.verification_level == "exact_step"
    assert res.verdict.goal_satisfied is True
    assert any("화력이 한 단계 올라갔습니다" in e.message for e in res.events)

    # 락 해제 확인
    new_res = client.post("/api/runs", json={"user_input": "화력 1단 올려줘", "source": "mock", "zone_id": "left"})
    assert new_res.status_code == 201


def test_run_step_requires_observing_status():
    """observing이 아닌 상태에서 step 호출 시 409 conflict 차단."""
    create_res = client.post("/api/runs", json={"user_input": "화력 1단 올려줘", "source": "mock", "zone_id": "right"})
    run_id = create_res.json()["run_id"]

    # 아직 awaiting_user 상태
    step_res = client.post(f"/api/runs/{run_id}/step")
    assert step_res.status_code == 409
    assert "observing 상태여야 합니다" in step_res.json()["message"]
