"""PR #16 review regressions through the public API; no external API calls."""
import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.adapters.mock import MockAdapter, VirtualClock
from app.adapters.mock_setup import build_mock_adapter
from app.agent.orchestrator import validate_instruction_against_goal
from app.api.runs import run_store
from app.config import settings
from app.contracts.models import Goal, Quality
from app.llm.base import LlmResponse, ToolCall
from app.llm.mock_provider import MockLlmProvider
from app.main import app
from app.tools.sensor_tools import CheckSensorIntegrityTool
from tests.test_misoperation_recovery import build_multi_step_mock_adapter


client = TestClient(app)
REQUEST = {"user_input": "오른쪽 화력 1단 올려줘", "source": "mock", "zone_id": "right"}
INITIAL = "오른쪽 화구의 + 버튼을 한 번 눌러 주세요."
RETRY = "오른쪽 화구의 + 버튼을 다시 한 번 눌러 주세요."


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    run_store.reset()
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "execution_mode", "mock_llm")
    monkeypatch.setattr(settings, "openai_api_key", "sk-fake-offline-test")
    # Any accidentally unmocked product request must fail, never use the network.
    monkeypatch.setattr(httpx.AsyncClient, "post", AsyncMock(side_effect=AssertionError("External API forbidden")))
    yield
    run_store.reset()


def create_run():
    response = client.post("/api/runs", json=REQUEST)
    assert response.status_code == 201
    return client.get(f"/api/runs/{response.json()['run_id']}").json()


def perform_step(run):
    path = f"/api/runs/{run['run_id']}"
    action = {"action_id": run["instruction"]["action_id"]}
    assert client.post(f"{path}/ack", json=action).status_code == 200
    response = client.post(f"{path}/step", json=action)
    assert response.status_code == 200
    return response.json()


UNSAFE_TEXTS = [
    "오른쪽 화구의 전원 버튼을 한 번 눌러 주세요. 화력이 올라갑니다.",
    "오른쪽 화구의 + 버튼을 한 번 5초 동안 길게 눌러 주세요.",
    "오른쪽 화구의 + 버튼을 11회 눌러 주세요.",
    "오른쪽 화구의 + 버튼을 한 번 누른 채 계속 유지하세요.",
    "오른쪽 화구의 + 버튼을 한 번 누르지 마세요.",
    "오른쪽 화구의 + 버튼을 한 번 눌러 좌측도 켜세요.",
    "오른쪽 화구의 + 버튼을 한 번 누르고 한 번 더 눌러 주세요.",
    "오른쪽 화구의 + 버튼을 한 번 누른 뒤 변화가 없으면 다시 눌러 주세요.",
]


@pytest.mark.parametrize("text", UNSAFE_TEXTS)
@pytest.mark.parametrize("recovery", [False, True], ids=["initial", "recovery"])
def test_only_single_server_action_reaches_api_and_recovery(monkeypatch, text, recovery):
    goal = Goal(zone_id="right", direction="increase", requested_steps=1)
    assert validate_instruction_against_goal(text, goal, recovery)[0] is False
    provider = MockLlmProvider()
    if recovery:
        provider.queue_text(INITIAL)
        monkeypatch.setattr("app.api.runs.resolve_adapter_for_run", lambda _: build_multi_step_mock_adapter())
    provider.queue_text(text)
    monkeypatch.setattr("app.api.runs.get_llm_provider", lambda **_: provider)
    run = create_run()
    if recovery:
        original_action = run["instruction"]["action_id"]
        run = perform_step(run)
        assert run["instruction"]["action_id"] != original_action
    assert run["status"] == "awaiting_user"
    assert run["instruction"]["text"] == (RETRY if recovery else INITIAL)
    assert all(text != e["message"] for e in run["events"])


BAD_MESSAGES = [
    ({"content": None, "refusal": "Cannot assist with this request."}, "stop"),
    ({"content": INITIAL, "refusal": "Cannot assist."}, "stop"),
    ({"content": None}, "stop"),
    ({"content": "   "}, "stop"),
    ({"content": INITIAL}, "length"),
    ({"content": INITIAL}, "content_filter"),
    ({"content": INITIAL}, None),
    ({"content": None, "tool_calls": []}, "tool_calls"),
]


def http_response(message, finish_reason):
    return httpx.Response(200, request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"), json={
        "model": "offline-http-model",
        "choices": [{"message": {"role": "assistant", **message}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 45, "completion_tokens": 18, "total_tokens": 63},
    })


@pytest.mark.parametrize("message,finish_reason", BAD_MESSAGES)
@pytest.mark.parametrize("recovery", [False, True], ids=["initial", "recovery"])
def test_refusal_empty_or_incomplete_http_response_fails_without_instruction(monkeypatch, message, finish_reason, recovery):
    monkeypatch.setattr(settings, "execution_mode", "real_llm")
    responses = [http_response(message, finish_reason)]
    if recovery:
        responses.insert(0, http_response({"content": INITIAL}, "stop"))
        monkeypatch.setattr("app.api.runs.resolve_adapter_for_run", lambda _: build_multi_step_mock_adapter())
    post = AsyncMock(side_effect=responses)
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    run = create_run()
    if recovery:
        assert run["status"] == "awaiting_user"
        run = perform_step(run)
    assert run["status"] == "failed"
    assert run["instruction"] is None
    assert any(e["kind"] == "error" and "LlmResponseFormatError" in e["message"] for e in run["events"])
    assert any("tokens: 63" in e["message"] for e in run["events"])
    assert not any(e["kind"] == ("recovery" if recovery else "instruction") for e in run["events"])
    assert run_store._active_run_id is None
    # Terminal state cannot be advanced into a mock-observation success.
    calls_before = post.await_count
    repeated = client.post(f"/api/runs/{run['run_id']}/step", json={"action_id": "stale-action"})
    assert repeated.json()["status"] == "failed"
    assert post.await_count == calls_before
    monkeypatch.setattr(settings, "execution_mode", "mock_llm")
    assert create_run()["status"] == "awaiting_user"


@pytest.mark.parametrize("kind", ["empty", "whitespace", "loop_exhausted"])
def test_mock_response_empty_or_tool_loop_exhaustion_is_not_default_action(monkeypatch, kind):
    provider = MockLlmProvider()
    if kind == "loop_exhausted":
        for _ in range(3):
            provider.queue_tool_call("check_sensor_integrity", {})
    else:
        provider.queue_response(LlmResponse(text=None if kind == "empty" else "   "))
    monkeypatch.setattr("app.api.runs.get_llm_provider", lambda **_: provider)
    run = create_run()
    assert run["status"] == "failed"
    assert run["instruction"] is None
    assert run_store._active_run_id is None
    if kind == "loop_exhausted":
        assert len([e for e in run["events"] if e["kind"] == "tool_call"]) == 3
        assert any("calls: 3" in e["message"] for e in run["events"])


@pytest.mark.parametrize("response", [
    LlmResponse(), LlmResponse(text="   "),
    LlmResponse(tool_calls=[ToolCall(id="unexpected", name="read_field", arguments={"zone_id": "right"})]),
])
def test_recovery_requires_final_text_even_with_mock_provider(monkeypatch, response):
    provider = MockLlmProvider()
    provider.queue_text(INITIAL)
    provider.queue_response(response)
    monkeypatch.setattr("app.api.runs.get_llm_provider", lambda **_: provider)
    monkeypatch.setattr("app.api.runs.resolve_adapter_for_run", lambda _: build_multi_step_mock_adapter())
    run = perform_step(create_run())
    assert run["status"] == "failed"
    assert run["instruction"] is None
    assert run_store._active_run_id is None
    assert not any(e["kind"] == "recovery" for e in run["events"])


def test_recovery_rechecks_sensors_after_model_call(monkeypatch):
    adapter = build_multi_step_mock_adapter()

    class DegradingProvider(MockLlmProvider):
        async def generate(self, messages, tools=None):
            if tools is None:
                adapter.check_sensor_integrity = lambda: Quality(status="degraded", reasons=["clock_unverified"])
                return LlmResponse(text=RETRY)
            return LlmResponse(text=INITIAL)

    monkeypatch.setattr("app.api.runs.get_llm_provider", lambda **_: DegradingProvider())
    monkeypatch.setattr("app.api.runs.resolve_adapter_for_run", lambda _: adapter)
    run = perform_step(create_run())
    assert run["status"] == "uncertain"
    assert run["instruction"] is None
    assert run["verdict"]["reason_codes"] == ["sensor_integrity_check_failed"]
    assert run_store._active_run_id is None
    assert any(e["kind"] == "tool_call" and "실행 실패" in e["message"] for e in run["events"])
    assert not any(e["kind"] == "recovery" for e in run["events"])


def test_normal_http_tool_response_then_final_text_is_supported(monkeypatch):
    monkeypatch.setattr(settings, "execution_mode", "real_llm")
    tool_response = http_response({"content": None, "tool_calls": [{
        "id": "check-1", "type": "function",
        "function": {"name": "check_sensor_integrity", "arguments": "{}"},
    }]}, "tool_calls")
    post = AsyncMock(side_effect=[tool_response, http_response({"content": INITIAL, "tool_calls": None}, "stop")])
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    run = create_run()
    assert run["status"] == "awaiting_user"
    assert run["instruction"]["text"] == INITIAL
    assert post.await_count == 2
    assert any("tokens: 126" in e["message"] for e in run["events"])
    tool_results = post.call_args.kwargs["json"]["messages"]
    assert any(m["role"] == "tool" and '"success": true' in m["content"] for m in tool_results)


@pytest.mark.parametrize("call_tools", [False, True])
@pytest.mark.parametrize("fault", [
    "none", "missing_samples", "clock_unverified", "saturated", "uncalibrated", "other_degraded",
    "missing_baseline", "missing_field", "stale_baseline", "unverified_baseline", "uncalibrated_baseline",
])
def test_required_preflight_cannot_be_skipped_by_model(monkeypatch, call_tools, fault):
    adapter = build_mock_adapter("normal")
    if fault == "missing_samples":
        adapter = MockAdapter([], VirtualClock(now_ms=500))
    elif fault in {"clock_unverified", "saturated", "uncalibrated", "other_degraded"}:
        monkeypatch.setattr(adapter, "check_sensor_integrity", lambda: Quality(status="degraded", reasons=[fault]))
    elif fault != "none":
        baseline = adapter.read_baseline("right").model_copy(deep=True)
        if fault == "missing_baseline":
            baseline = None
        elif fault == "missing_field":
            baseline.device_observation.field_summary = None
        elif fault == "stale_baseline":
            adapter.clock.advance_to(1001)
        elif fault == "unverified_baseline":
            baseline.clock_quality = "unverified"
        elif fault == "uncalibrated_baseline":
            baseline.device_observation.calibration_id = None
        monkeypatch.setattr(adapter, "read_baseline", lambda _: baseline)
    provider = MockLlmProvider()
    if call_tools:
        provider.queue_tool_call("check_sensor_integrity", {})
    provider.queue_text(INITIAL)
    monkeypatch.setattr("app.api.runs.resolve_adapter_for_run", lambda _: adapter)
    monkeypatch.setattr("app.api.runs.get_llm_provider", lambda **_: provider)
    run = create_run()
    events = [e for e in run["events"] if e["kind"] == "tool_call"]
    assert any("check_sensor_integrity" in e["message"] for e in events)
    assert any("read_field" in e["message"] for e in events)
    if fault == "none":
        assert run["status"] == "awaiting_user"
        assert run["instruction"]["text"] == INITIAL
        assert not any("실행 실패" in e["message"] for e in events)
    else:
        assert run["status"] == "uncertain"
        assert run["instruction"] is None
        assert run["verdict"]["reason_codes"] == ["sensor_integrity_check_failed"]
        assert not any(e["kind"] == "instruction" for e in run["events"])
        assert run_store._active_run_id is None


@pytest.mark.parametrize("changes", [
    {"clock_sync": "unsynced"}, {"saturation": True}, {"calibrated": False},
    {"quality": "degraded", "reasons": ["clock_unverified"]},
    {"quality": "degraded", "reasons": []}, {"reasons": ["unknown_quality_problem"]},
    {"clock_sync": None},
])
def test_integrity_tool_requires_all_safety_conditions(changes):
    data = dict(connected=True, saturation=False, calibrated=True, clock_sync="verified", quality="valid", reasons=[])
    data.update(changes)
    result = asyncio.run(CheckSensorIntegrityTool(provider=lambda: data).execute())
    assert result.success is False


@pytest.mark.parametrize("zone,label", [("left", "왼쪽"), ("right", "오른쪽")])
def test_safe_template_uses_requested_zone(monkeypatch, zone, label):
    provider = MockLlmProvider()
    provider.queue_text(f"{label} 화구의 + 버튼을 한 번 눌러 주세요.")
    monkeypatch.setattr("app.api.runs.get_llm_provider", lambda **_: provider)
    response = client.post("/api/runs", json={**REQUEST, "user_input": f"{label} 화력 1단 올려줘", "zone_id": zone})
    run = client.get(f"/api/runs/{response.json()['run_id']}").json()
    assert run["status"] == "awaiting_user"
    assert run["instruction"]["text"] == f"{label} 화구의 + 버튼을 한 번 눌러 주세요."
