"""Tests for Execution API across execution modes (mock_llm, real_llm) and 3 scenarios (C-YD-04)."""
import asyncio
from datetime import datetime, timedelta, timezone
import threading
import time
from unittest.mock import AsyncMock, patch
import httpx
import pytest
from fastapi.testclient import TestClient

from tests.test_misoperation_recovery import build_multi_step_mock_adapter
from app.agent.orchestrator import AgentOrchestrator
from app.api.runs import run_store, run_sync
from app.config import settings
from app.llm.base import LlmResponse, LlmUsage, ToolCall
from app.llm.mock_provider import MockLlmProvider
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


def test_real_llm_mode_http_errors_yield_failed_without_instruction():
    """[P1 회귀] real_llm에서 HTTP 401, 429, Timeout 발생 시 조작 안내 없이 failed로 즉시 안전 종료되어야 함."""
    settings.execution_mode = "real_llm"
    settings.openai_api_key = "sk-fake-key"

    test_cases = [
        ("401", 401, "Unauthorized"),
        ("429", 429, "Rate limit reached"),
        ("timeout", None, "Read timed out"),
    ]

    for name, status_code, err_text in test_cases:
        run_store.reset()

        if status_code is not None:
            mock_resp = httpx.Response(
                status_code=status_code,
                request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
                text=err_text,
            )
            side_effect = httpx.HTTPStatusError("HTTP Error", request=mock_resp.request, response=mock_resp)
        else:
            side_effect = httpx.ReadTimeout("Timeout")

        with patch("httpx.AsyncClient.post", side_effect=side_effect):
            res = client.post(
                "/api/runs",
                json={"user_input": "오른쪽 화력 1단 올려줘", "source": "mock", "zone_id": "right"},
            )
            assert res.status_code == 201
            data = res.json()
            run_id = data["run_id"]

            # 조작 대기(awaiting_user)가 아닌 failed로 명시적 종료
            assert data["status"] == "failed"
            assert data["execution_mode"] == "real_llm"

            details = client.get(f"/api/runs/{run_id}").json()
            assert details["status"] == "failed"
            assert details["instruction"] is None
            assert any(e["kind"] == "error" for e in details["events"])

            # 활성 실행 락이 해제되어 새 실행 생성이 가능해야 함
            new_run_res = client.post(
                "/api/runs",
                json={"user_input": "오른쪽 화력 1단 올려줘", "source": "mock", "zone_id": "right"},
            )
            assert new_run_res.status_code == 201  # busy(409)가 아니어야 함

            # 종료된 실행에 대해 ack 시도 시 terminal_run으로 차단
            ack_res = client.post(f"/api/runs/{run_id}/ack", json={"action_id": "act-fake"})
            assert ack_res.status_code == 409
            assert ack_res.json()["code"] == "terminal_run"


def test_api_mismatched_instruction_replaced_with_safe_fallback():
    """[P1 회귀] API 경로에서 LLM이 잘못된 화구·방향·횟수를 생성하더라도 안전 기본 안내로 대체되어야 함."""
    settings.execution_mode = "mock_llm"

    mock_llm = MockLlmProvider()
    # P1 리뷰어 재현 케이스: 오른쪽 요청에 왼쪽 화구 + 5번 누름 응답
    mock_llm.queue_text("왼쪽 화구의 + 버튼을 다섯 번 눌러 주세요.")

    with patch("app.api.runs.get_llm_provider", return_value=mock_llm):
        create_res = client.post(
            "/api/runs",
            json={"user_input": "오른쪽 화력 1단 올려줘", "source": "mock", "zone_id": "right"},
        )
        assert create_res.status_code == 201
        run_data = create_res.json()
        assert run_data["status"] == "awaiting_user"

        run_id = run_data["run_id"]
        details = client.get(f"/api/runs/{run_id}").json()
        # 잘못된 '왼쪽 화구/다섯 번'이 아닌 안전 기본 안내가 사용자에게 제공되어야 함
        assert details["instruction"]["text"] == "오른쪽 화구의 + 버튼을 한 번 눌러 주세요."
        assert any(
            e["kind"] == "plan" and "안전 검증 불일치" in e["message"]
            for e in details["events"]
        )


def test_create_run_planning_concurrency_and_timeout_enforced():
    """[P1 회귀] run_sync 타임아웃 강제 및 저장소 잠금 밖 계획 실행으로 인한 비차단 GET/Cancel 검증."""
    # 1. run_sync 시간 제한 강제 검증
    async def slow_func():
        await asyncio.sleep(0.15)
        return "done"

    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        run_sync(slow_func(), timeout=0.03)

    # 2. 계획 도중 다른 스레드에서 GET /api/runs/{run_id}를 호출할 때 잠금에 블로킹되지 않고 planning 반환 확인
    settings.execution_mode = "mock_llm"
    delay_started = threading.Event()
    allow_finish = threading.Event()

    class DelayingProvider(MockLlmProvider):
        async def generate(self, *args, **kwargs):
            delay_started.set()
            # 계획 생성 중에 대기
            allow_finish.wait(timeout=2.0)
            return await super().generate(*args, **kwargs)

    with patch("app.api.runs.get_llm_provider", return_value=DelayingProvider()):
        post_thread_result: dict[str, Any] = {}

        def run_post():
            resp = client.post(
                "/api/runs",
                json={"user_input": "오른쪽 화력 1단 올려줘", "source": "mock", "zone_id": "right"},
            )
            post_thread_result["resp"] = resp

        th = threading.Thread(target=run_post)
        th.start()

        # LLM 계획 시작 대기
        assert delay_started.wait(timeout=1.0)

        # 계획 진행 중 활성 실행 ID를 확인하고 GET 호출 -> 잠금에 막히지 않고 즉시 planning 상태 반환
        active_id = run_store._active_run_id
        assert active_id is not None
        get_res = client.get(f"/api/runs/{active_id}")
        assert get_res.status_code == 200
        assert get_res.json()["status"] == "planning"

        # 계획 완료 허용
        allow_finish.set()
        th.join(timeout=2.0)

        assert "resp" in post_thread_result
        assert post_thread_result["resp"].status_code == 201
        assert post_thread_result["resp"].json()["status"] == "awaiting_user"


def test_create_run_cancelled_during_planning():
    """[P1 회귀] 계획 진행 도중 취소(cancel)가 발생하면 계획 완료 후에도 stopped가 유지되어야 함."""
    settings.execution_mode = "mock_llm"
    delay_started = threading.Event()
    allow_finish = threading.Event()

    class DelayingProvider(MockLlmProvider):
        async def generate(self, *args, **kwargs):
            delay_started.set()
            allow_finish.wait(timeout=2.0)
            return await super().generate(*args, **kwargs)

    with patch("app.api.runs.get_llm_provider", return_value=DelayingProvider()):
        th = threading.Thread(
            target=lambda: client.post(
                "/api/runs",
                json={"user_input": "오른쪽 화력 1단 올려줘", "source": "mock", "zone_id": "right"},
            )
        )
        th.start()
        assert delay_started.wait(timeout=1.0)

        active_id = run_store._active_run_id
        # 계획 도중 cancel 호출
        cancel_res = client.post(f"/api/runs/{active_id}/cancel")
        assert cancel_res.status_code == 200
        assert cancel_res.json()["status"] == "stopped"

        allow_finish.set()
        th.join(timeout=2.0)

        # 계획이 끝난 뒤에도 세대 불일치로 인해 awaiting_user로 덮어쓰이지 않고 stopped 유지
        final_res = client.get(f"/api/runs/{active_id}").json()
        assert final_res["status"] == "stopped"


def test_create_run_status_updated_at_refreshed_on_instruction_post():
    """[P1 회귀] 계획 수립에 시간이 걸리더라도 안내 게시 시점에 status_updated_at이 갱신되어 첫 GET에서 즉시 stopped되지 않아야 함."""
    settings.execution_mode = "mock_llm"

    # 가상 시계 설정 (계획 시작 시각: t0)
    t0 = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
    current_time = t0

    def get_time():
        return current_time

    run_store.set_clock(get_time)

    mock_llm = MockLlmProvider()

    # 계획 생성 중에 가상 시계가 61초 경과했다고 가정
    orig_generate = mock_llm.generate

    async def delayed_generate(*args, **kwargs):
        nonlocal current_time
        current_time += timedelta(seconds=61)
        return await orig_generate(*args, **kwargs)

    mock_llm.generate = delayed_generate

    with patch("app.api.runs.get_llm_provider", return_value=mock_llm):
        create_res = client.post(
            "/api/runs",
            json={"user_input": "오른쪽 화력 1단 올려줘", "source": "mock", "zone_id": "right"},
        )
        assert create_res.status_code == 201
        run_data = create_res.json()
        assert run_data["status"] == "awaiting_user"

        run_id = run_data["run_id"]
        # 첫 GET에서 status_updated_at이 갱신되었으므로 awaiting_user가 유지되어야 함 (stopped로 즉시 만료되지 않음)
        get_res = client.get(f"/api/runs/{run_id}").json()
        assert get_res["status"] == "awaiting_user"


def test_create_run_tools_connected_to_mock_adapter_and_sensor_error():
    """[P2 회귀] 생성 API에서 사전 점검 Tool이 해당 실행의 어댑터에 연결되며 센서 결함 시 안전 중단됨을 검증."""
    settings.execution_mode = "mock_llm"

    # 1. 정상 Mock 출처에서 check_sensor_integrity 및 read_field 호출 시 실제 어댑터 연결 검증
    mock_llm = MockLlmProvider()
    mock_llm.queue_response(
        LlmResponse(
            text=None,
            tool_calls=[
                ToolCall(id="tc-1", name="check_sensor_integrity", arguments={}),
                ToolCall(id="tc-2", name="read_field", arguments={"zone_id": "right"}),
            ],
            usage=LlmUsage(prompt_tokens=30, completion_tokens=10, total_tokens=40),
        )
    )
    mock_llm.queue_response(
        LlmResponse(
            text="오른쪽 화구의 + 버튼을 한 번 눌러 주세요.",
            tool_calls=[],
            usage=LlmUsage(prompt_tokens=40, completion_tokens=15, total_tokens=55),
        )
    )

    with patch("app.api.runs.get_llm_provider", return_value=mock_llm):
        res1 = client.post(
            "/api/runs",
            json={"user_input": "오른쪽 화력 1단 올려줘", "source": "mock", "zone_id": "right"},
        )
        assert res1.status_code == 201
        run1 = res1.json()
        assert run1["status"] == "awaiting_user"

        events = client.get(f"/api/runs/{run1['run_id']}").json()["events"]
        # 실제 어댑터와 연결되어 센서 미설정 오류 없이 실행 완료가 기록되어야 함
        assert any(e["kind"] == "tool_call" and "도구 'check_sensor_integrity' 실행 완료" in e["message"] for e in events)
        assert any(e["kind"] == "tool_call" and "도구 'read_field' 실행 완료" in e["message"] for e in events)

    # 2. 센서 에러 어댑터 주입 시: check_sensor_integrity가 실패하고 조작 안내 차단(uncertain) 검증
    run_store.reset()
    from app.adapters.mock import MockAdapter, VirtualClock

    # 사전 기준 신호가 없는 결함 어댑터 (missing_samples -> invalid)
    faulty_adapter = MockAdapter([], VirtualClock(now_ms=500))

    mock_llm_faulty = MockLlmProvider()
    mock_llm_faulty.queue_response(
        LlmResponse(
            text=None,
            tool_calls=[ToolCall(id="tc-fault", name="check_sensor_integrity", arguments={})],
            usage=LlmUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30),
        )
    )

    with patch("app.api.runs.resolve_adapter_for_run", return_value=faulty_adapter):
        with patch("app.api.runs.get_llm_provider", return_value=mock_llm_faulty):
            res2 = client.post(
                "/api/runs",
                json={"user_input": "오른쪽 화력 1단 올려줘", "source": "mock", "zone_id": "right"},
            )
            assert res2.status_code == 201
            run2 = res2.json()

            # 센서 무결성 실패로 인해 조작 안내 없이 uncertain으로 안전 종료
            assert run2["status"] == "uncertain"

            details2 = client.get(f"/api/runs/{run2['run_id']}").json()
            assert details2["status"] == "uncertain"
            assert details2["instruction"] is None
            events2 = details2["events"]
            assert any(e["kind"] == "tool_call" and "실행 실패" in e["message"] for e in events2)
            assert any(e["kind"] == "error" and "센서 사전 점검에서 이상" in e["message"] for e in events2)
            # 센서 사전 점검 실패로 uncertain 종료되어도 이미 소비된 토큰 메트릭이 보존되어야 함
            assert any(e["kind"] == "plan" and "LLM 계획 토큰 메트릭" in e["message"] and "tokens:" in e["message"] for e in events2)
            # 활성 실행 락도 해제되어야 함
            assert run_store._active_run_id is None


def test_real_llm_response_schema_or_json_error_terminates_failed():
    """[P2 회귀] real_llm 모드에서 모델이 잘못된 JSON이나 빈 choices를 반환하면 fallback 없이 failed로 안전 종료되어야 함."""
    settings.execution_mode = "real_llm"
    settings.openai_api_key = "sk-valid-format-key-for-test"

    malformed_responses = [
        ("invalid_json", "not a valid json response {"),
        ("empty_choices", '{"choices": []}'),
    ]

    for label, raw_text in malformed_responses:
        run_store.reset()

        mock_resp = httpx.Response(
            status_code=200,
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
            text=raw_text,
        )

        with patch("httpx.AsyncClient.post", return_value=mock_resp):
            res = client.post(
                "/api/runs",
                json={"user_input": "오른쪽 화력 1단 올려줘", "source": "mock", "zone_id": "right"},
            )
            assert res.status_code == 201
            run_data = res.json()
            run_id = run_data["run_id"]

            # 모델 응답 파싱/스키마 실패 시 awaiting_user로 넘어가지 않고 failed로 종료
            assert run_data["status"] == "failed"
            assert run_data["execution_mode"] == "real_llm"

            details = client.get(f"/api/runs/{run_id}").json()
            assert details["status"] == "failed"
            assert details["instruction"] is None
            assert any(e["kind"] == "error" and ("LlmResponseFormatError" in e["message"] or "파싱 실패" in e["message"] or "choices" in e["message"]) for e in details["events"])


def test_first_call_success_second_call_fatal_error_preserves_usage_and_tools():
    """[P2 회귀] 첫 번째 호출에서 도구 실행과 50토큰이 소비된 뒤 두 번째 호출에서 LlmRateLimitError가 발생해도 사용량과 도구 기록이 보존되어야 함."""
    run_store.reset()
    settings.execution_mode = "mock_llm"

    from app.llm.openai_provider import LlmRateLimitError

    class TwoTurnFailingProvider(MockLlmProvider):
        def __init__(self):
            super().__init__()
            self.turn = 0

        async def generate(self, messages, tools=None):
            self.turn += 1
            if self.turn == 1:
                return LlmResponse(
                    text=None,
                    tool_calls=[ToolCall(id="tc-1", name="check_sensor_integrity", arguments={})],
                    usage=LlmUsage(prompt_tokens=35, completion_tokens=15, total_tokens=50),
                    model="mock-two-turn",
                )
            raise LlmRateLimitError("429 Too Many Requests")

    failing_provider = TwoTurnFailingProvider()

    with patch("app.api.runs.get_llm_provider", return_value=failing_provider):
        res = client.post(
            "/api/runs",
            json={"user_input": "오른쪽 화력 1단 올려줘", "source": "mock", "zone_id": "right"},
        )
        assert res.status_code == 201
        run_data = res.json()
        assert run_data["status"] == "failed"

        details = client.get(f"/api/runs/{run_data['run_id']}").json()
        assert details["status"] == "failed"
        assert details["instruction"] is None
        events = details["events"]

        # 첫 번째 턴에서 성공한 도구 실행 내역이 보존되어야 함
        assert any(e["kind"] == "tool_call" and "도구 'check_sensor_integrity' 실행 완료" in e["message"] for e in events)

        # 첫 번째 턴에서 소비된 50토큰 메트릭이 보존되어야 함
        assert any(e["kind"] == "plan" and "LLM 계획 토큰 메트릭" in e["message"] and "tokens: 50" in e["message"] for e in events)

        # 두 번째 턴의 실패 원인이 error 이벤트로 기록되어야 함
        assert any(e["kind"] == "error" and "LlmRateLimitError" in e["message"] for e in events)


def test_recovery_event_and_speech_items_carry_verified_instruction():
    """[P2 회귀] 오조작 복구 응답에서 recovery 및 instruction 이벤트에 사용자가 음성으로 안내받을 실제 조작 문장이 전달되어야 함."""
    run_store.reset()
    settings.execution_mode = "mock_llm"

    from tests.test_misoperation_recovery import build_multi_step_mock_adapter

    adapter_nc = build_multi_step_mock_adapter(second_step_success=True)

    mock_llm = MockLlmProvider()
    # 1차 계획 생성 응답
    mock_llm.queue_text("오른쪽 화구의 + 버튼을 한 번 눌러 주세요.")
    # 복구 재계획 응답
    mock_llm.queue_text("출력 변화가 없습니다. 오른쪽 화구의 + 버튼을 다시 한 번 꼭 눌러 주세요.")

    with patch("app.api.runs.resolve_adapter_for_run", return_value=adapter_nc):
        with patch("app.api.runs.get_llm_provider", return_value=mock_llm):
            # 1. 초기 실행 생성
            c_res = client.post(
                "/api/runs",
                json={"user_input": "오른쪽 화력 1단 올려줘", "source": "mock", "zone_id": "right"},
            )
            assert c_res.status_code == 201
            run_id = c_res.json()["run_id"]

            details1 = client.get(f"/api/runs/{run_id}").json()
            act1 = details1["instruction"]["action_id"]

            # 2. 사용자 조작 인지 (ack)
            client.post(f"/api/runs/{run_id}/ack", json={"action_id": act1})

            # 3. 관측 수행 -> no_change로 인한 mismatch -> 1회 복구 트리거
            step_res = client.post(f"/api/runs/{run_id}/step", json={"action_id": act1})
            assert step_res.status_code == 200
            step_data = step_res.json()

            # 복구 후 새로운 action_id 발급 및 awaiting_user 전이
            assert step_data["status"] == "awaiting_user"
            act2 = step_data["instruction"]["action_id"]
            assert act2 != act1
            assert "다시 한 번 꼭" in step_data["instruction"]["text"]

            events = step_data["events"]

            # recovery 이벤트의 message는 'LLM 재조작 안내 수립 완료'가 아니라 실제 사용자 조작 안내 문장이어야 함
            recovery_events = [e for e in events if e["kind"] == "recovery" and e.get("action_id") == act2]
            assert len(recovery_events) == 1
            assert recovery_events[0]["message"] == step_data["instruction"]["text"]
            assert "오른쪽 화구의 + 버튼을 다시 한 번 꼭 눌러 주세요." in recovery_events[0]["message"]

            # 내부 계획 완료 설명은 plan 이벤트로 안전하게 분리되어야 함
            plan_events = [e for e in events if e["kind"] == "plan" and e.get("action_id") == act2]
            assert any("LLM 재조작 안내 수립 완료" in e["message"] for e in plan_events)
