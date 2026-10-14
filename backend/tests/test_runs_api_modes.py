"""Tests for Execution API across execution modes (mock_llm, real_llm) and 3 scenarios (C-YD-04)."""
import asyncio
from unittest.mock import AsyncMock, patch
import pytest
from fastapi.testclient import TestClient

from tests.test_misoperation_recovery import build_multi_step_mock_adapter
from app.agent.orchestrator import AgentOrchestrator
from app.api.runs import run_store
from app.config import settings
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_store_and_mode():
    run_store.reset()
    orig_mode = settings.execution_mode
    orig_key = settings.openai_api_key
    yield
    run_store.reset()
    settings.execution_mode = orig_mode
    settings.openai_api_key = orig_key


def test_mock_llm_mode_scenario_1_exact_step_success():
    """시나리오 1: mock_llm 모드에서 목표 수신 -> 사전 점검 -> 안내 -> 관측 -> 성공 완주."""
    settings.execution_mode = "mock_llm"

    # 1. Create run
    create_res = client.post(
        "/api/runs",
        json={"user_input": "오른쪽 화력 1단 올려줘", "source": "mock", "zone_id": "right"},
    )
    assert create_res.status_code == 201
    run_info = create_res.json()
    run_id = run_info["run_id"]
    assert run_info["execution_mode"] == "mock_llm"
    assert run_info["status"] == "awaiting_user"

    # Verify events include plan and instruction
    get_res = client.get(f"/api/runs/{run_id}").json()
    action_id = get_res["instruction"]["action_id"]
    events = get_res["events"]
    assert any(e["kind"] == "instruction" for e in events)
    assert any(e["kind"] == "plan" and "토큰 메트릭" in e["message"] for e in events)

    # 2. Ack instruction
    ack_res = client.post(f"/api/runs/{run_id}/ack", json={"action_id": action_id})
    assert ack_res.status_code == 200
    assert ack_res.json()["status"] == "observing"

    # 3. Step observation (in mock_llm mode, normal mock adapter is automatically injected)
    step_res = client.post(f"/api/runs/{run_id}/step", json={"action_id": action_id})
    assert step_res.status_code == 200
    step_data = step_res.json()

    assert step_data["status"] == "succeeded"
    assert step_data["verdict"]["status"] == "success"
    assert step_data["verdict"]["verification_level"] == "exact_step"
    assert step_data["verdict"]["goal_satisfied"] is True
    # Verify verdict event and token metric event
    step_events = step_data["events"]
    assert any(e["kind"] == "verdict" and "화력이 한 단계" in e["message"] for e in step_events)


def test_mock_llm_mode_scenario_2_no_change_recovery_lifecycle():
    """시나리오 2: mock_llm 모드에서 1차 변화 없음 -> 오조작 복구 스킬 1회 재조작 안내 -> 2차 성공."""
    settings.execution_mode = "mock_llm"

    # 1. Create run
    create_res = client.post(
        "/api/runs",
        json={"user_input": "오른쪽 화력 1단 올려줘", "source": "mock", "zone_id": "right"},
    )
    run_id = create_res.json()["run_id"]
    run_info = client.get(f"/api/runs/{run_id}").json()
    action_1 = run_info["instruction"]["action_id"]

    # 2. Ack action 1
    client.post(f"/api/runs/{run_id}/ack", json={"action_id": action_1})

    # 3. Step 1 with multi-step adapter injected (1st step yields no_change, but has fresh baseline for retry)
    multi_adapter = build_multi_step_mock_adapter(second_step_success=True)
    recovery_orch = AgentOrchestrator(adapter=multi_adapter)

    step_1 = asyncio.run(
        run_store.execute_observation_step(run_id, orchestrator=recovery_orch, action_id=action_1)
    )

    # Status should transition to awaiting_user with a new action_id
    assert step_1.status == "awaiting_user"
    assert step_1.instruction is not None
    action_2 = step_1.instruction.action_id
    assert action_2 != action_1
    assert any(e.kind == "recovery" for e in step_1.events)

    # 4. Ack action 2
    ack_res2 = client.post(f"/api/runs/{run_id}/ack", json={"action_id": action_2})
    assert ack_res2.status_code == 200
    assert ack_res2.json()["status"] == "observing"

    # 5. Step 2 with same multi-step adapter -> 2nd step yields increase (RMS 120.0) -> success
    step_2 = asyncio.run(
        run_store.execute_observation_step(run_id, orchestrator=recovery_orch, action_id=action_2)
    )
    assert step_2.status == "succeeded"
    assert step_2.verdict.status == "success"
    assert step_2.verdict.goal_satisfied is True


def test_mock_llm_mode_scenario_3_sensor_error_uncertain_stop():
    """시나리오 3: mock_llm 모드에서 센서 이상 시 재조작 없이 즉시 uncertain 안전 중단."""
    settings.execution_mode = "mock_llm"
    from app.adapters.mock_setup import build_mock_adapter

    # 1. Create run & Ack
    create_res = client.post(
        "/api/runs",
        json={"user_input": "오른쪽 화력 1단 올려줘", "source": "mock", "zone_id": "right"},
    )
    run_id = create_res.json()["run_id"]
    action_id = client.get(f"/api/runs/{run_id}").json()["instruction"]["action_id"]
    client.post(f"/api/runs/{run_id}/ack", json={"action_id": action_id})

    # 2. Step with sensor_error adapter
    error_orch = AgentOrchestrator(
        adapter=build_mock_adapter("sensor_error", zone_id="right")
    )
    step_res = asyncio.run(
        run_store.execute_observation_step(run_id, orchestrator=error_orch, action_id=action_id)
    )

    assert step_res.status == "uncertain"
    assert step_res.verdict.status == "uncertain"
    assert step_res.verdict.goal_satisfied is None
    # No recovery instruction should be issued
    assert not any(e.kind == "recovery" for e in step_res.events)


def test_real_llm_mode_fails_safely_when_key_missing():
    """real_llm 모드에서 OPENAI_API_KEY가 없으면 모의 모델로 속이지 않고 명시적 오류(provider_waiting)로 중단."""
    settings.execution_mode = "real_llm"
    settings.openai_api_key = ""

    response = client.post(
        "/api/runs",
        json={"user_input": "오른쪽 화력 1단 올려줘", "source": "mock", "zone_id": "right"},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["execution_mode"] == "real_llm"
    assert data["status"] == "failed"

    run_info = client.get(f"/api/runs/{data['run_id']}").json()
    assert run_info["status"] == "failed"
    assert run_info["instruction"] is None
    # Verify explicit error event
    assert any(
        e["kind"] == "error" and "provider_waiting" in e["message"]
        for e in run_info["events"]
    )


def test_real_llm_mode_with_mocked_http_success():
    """real_llm 모드에서 API 키가 있고 성공적인 OpenAI 응답을 수신했을 때의 E2E 동작 검증."""
    settings.execution_mode = "real_llm"
    settings.openai_api_key = "sk-test-valid-key"

    fake_response = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": "gpt-6-luna",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "오른쪽 화구의 + 버튼을 한 번 눌러 주세요.",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 45,
            "completion_tokens": 18,
            "total_tokens": 63,
            "completion_tokens_details": {"reasoning_tokens": 0},
        },
    }

    class FakeHttpResponse:
        status_code = 200
        def json(self):
            return fake_response
        def raise_for_status(self):
            pass

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = FakeHttpResponse()

        create_res = client.post(
            "/api/runs",
            json={"user_input": "오른쪽 화력 1단 올려줘", "source": "mock", "zone_id": "right"},
        )
        assert create_res.status_code == 201
        run_data = create_res.json()
        assert run_data["status"] == "awaiting_user"
        assert run_data["execution_mode"] == "real_llm"

        run_details = client.get(f"/api/runs/{run_data['run_id']}").json()
        assert run_details["instruction"]["text"] == "오른쪽 화구의 + 버튼을 한 번 눌러 주세요."
        events = run_details["events"]
        assert any("gpt-6-luna" in e["message"] and "tokens: 63" in e["message"] for e in events)
