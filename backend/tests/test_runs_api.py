"""Tests for Execution API (C-YD-00)."""
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
    payload = {"user_input": "인덕션 켜줘", "source": "mock", "zone_id": None}
    response = client.post("/api/runs", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "awaiting_clarification"

    get_res = client.get(f"/api/runs/{data['run_id']}")
    assert get_res.status_code == 200
    run_data = get_res.json()
    assert run_data["instruction"] is None
    assert any("화구가 지정되지 않았습니다" in e["message"] for e in run_data["events"])


def test_single_active_run_concurrency_lock():
    payload1 = {"user_input": "화력 올려줘", "source": "mock", "zone_id": "right"}
    res1 = client.post("/api/runs", json=payload1)
    assert res1.status_code == 201

    payload2 = {"user_input": "다른 화구 켜줘", "source": "mock", "zone_id": "left"}
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
    create_res2 = client.post("/api/runs", json={"user_input": "새로운 조작", "source": "mock", "zone_id": "left"})
    assert create_res2.status_code == 201


def test_ack_on_terminal_run_returns_409():
    create_res = client.post("/api/runs", json={"user_input": "화력 올려줘", "source": "mock", "zone_id": "right"})
    run_id = create_res.json()["run_id"]
    client.post(f"/api/runs/{run_id}/cancel")

    # Now run is stopped (terminal)
    ack_res = client.post(f"/api/runs/{run_id}/ack", json={"action_id": "act-any"})
    assert ack_res.status_code == 409
    assert ack_res.json()["code"] == "terminal_run"


def test_validation_error_format():
    # Missing required field user_input
    response = client.post("/api/runs", json={"source": "mock"})
    assert response.status_code == 422
    err = response.json()
    assert err["code"] == "validation"
    assert "요청 형식" in err["message"]
