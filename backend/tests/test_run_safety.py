"""Safety regressions: real store/API paths, with explicit Mock-only workers."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading

import pytest
from fastapi.testclient import TestClient

import app.api.runs as runs
from app.agent.orchestrator import AgentOrchestrator
from app.contracts.goal_policy import interpret_p0_goal
from app.contracts.models import AckRequest, CreateRunRequest, Verdict
from app.main import app

CASES = json.loads((Path(__file__).resolve().parents[2] / "tests/integration/goal-cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["text"])
def test_shared_goal_gate_in_api_and_agent(case):
    store = runs.RunStore()
    request = CreateRunRequest(user_input=case["text"], source="mock", zone_id=case["selected"])
    goal, error = interpret_p0_goal(request.user_input, request.zone_id)
    assert AgentOrchestrator().interpret_goal(request.user_input, request.zone_id) == (goal, error)
    created = store.create_run(request)
    run = store.get_run(created.run_id)
    if case["zone"] is None:
        assert goal is None and error
        assert run.status == "awaiting_clarification"
        assert run.instruction is None
        assert not any(event.kind == "instruction" for event in run.events)
    else:
        assert goal.zone_id == case["zone"] and goal.requested_steps == 1
        assert run.status == "awaiting_user"
        assert ("왼쪽" if case["zone"] == "left" else "오른쪽") in run.instruction.text


def observing(store):
    created = store.create_run(CreateRunRequest(user_input="화력 한 단계 올려줘", source="mock", zone_id="right"))
    action = store.get_run(created.run_id).instruction.action_id
    store.ack_run(created.run_id, AckRequest(action_id=action))
    return created.run_id, action


def assert_unlocked(store):
    assert store.create_run(CreateRunRequest(user_input="화력 올려줘", source="mock", zone_id="left")).status == "awaiting_user"


class BlockingWorker(AgentOrchestrator):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    async def execute_observation_and_verification(self, **kwargs):
        self.calls += 1
        self.started.set()
        assert self.release.wait(2), "test worker was not released"
        return Verdict(status="uncertain", verification_level="none", goal_satisfied=None,
                       reason_codes=["mock_test"], evidence_ids=[]), "Mock test only"


def test_duplicate_step_ack_poll_and_cancel_race_preserve_terminal():
    async def scenario():
        store = runs.RunStore()
        run_id, action = observing(store)
        worker = BlockingWorker()
        pending = asyncio.create_task(store.execute_observation_step(run_id, worker, action))
        assert await asyncio.to_thread(worker.started.wait, 1)
        try:
            assert store.ack_run(run_id, AckRequest(action_id=action)).status == "observing"
            assert store.get_run(run_id).status == "observing"
            assert (await store.execute_observation_step(run_id, worker, action)).status == "observing"
            assert worker.calls == 1
            store.cancel_run(run_id)
            before = store.get_run(run_id).model_dump()
            assert_unlocked(store)
        finally:
            worker.release.set()
        assert (await pending).status == "stopped"
        assert store.get_run(run_id).model_dump() == before
        assert (await store.execute_observation_step(run_id, worker, action)).status == "stopped"
        assert worker.calls == 1
    asyncio.run(scenario())


def test_observing_expires_without_step_and_new_create_unlocks():
    now = [datetime.now(timezone.utc)]
    store = runs.RunStore(clock=lambda: now[0])
    run_id, action = observing(store)
    now[0] += timedelta(days=2)
    assert_unlocked(store)  # creation expires old run, even without polling
    run = store.get_run(run_id)
    assert run.status == "uncertain"
    assert run.verdict.reason_codes == ["observation_timeout"]
    assert run.verdict.evidence_ids == []
    assert store.ack_run(run_id, AckRequest(action_id=action)).status == "uncertain"


def test_observation_worker_timeout_late_result_is_discarded(monkeypatch):
    monkeypatch.setattr(runs, "TIMEOUT_OBSERVING_SECONDS", 0.02)
    async def scenario():
        store = runs.RunStore()
        run_id, action = observing(store)
        worker = BlockingWorker()
        try:
            result = await store.execute_observation_step(run_id, worker, action)
            assert result.status == "uncertain"
            assert result.verdict.reason_codes == ["observation_timeout"]
            before = store.get_run(run_id).model_dump()
            assert_unlocked(store)
        finally:
            worker.release.set()
        await asyncio.sleep(0.03)
        assert store.get_run(run_id).model_dump() == before
        assert worker.calls == 1
    asyncio.run(scenario())


def test_observation_failure_terminates_without_exception_details():
    class FailingWorker(AgentOrchestrator):
        async def execute_observation_and_verification(self, **kwargs):
            raise RuntimeError("private diagnostic sentinel")
    store = runs.RunStore()
    run_id, action = observing(store)
    result = asyncio.run(store.execute_observation_step(run_id, FailingWorker(), action))
    assert result.status == "uncertain"
    assert result.verdict.reason_codes == ["observation_processing_error"]
    assert result.verdict.evidence_ids == []
    assert "private diagnostic sentinel" not in result.model_dump_json()
    assert_unlocked(store)


def test_disconnected_step_task_unlocks_without_late_overwrite():
    async def scenario():
        store = runs.RunStore()
        run_id, action = observing(store)
        worker = BlockingWorker()
        task = asyncio.create_task(store.execute_observation_step(run_id, worker, action))
        assert await asyncio.to_thread(worker.started.wait, 1)
        try:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            before = store.get_run(run_id).model_dump()
            assert before["status"] == "uncertain"
            assert_unlocked(store)
        finally:
            worker.release.set()
        await asyncio.sleep(0.03)
        assert store.get_run(run_id).model_dump() == before
    asyncio.run(scenario())


@pytest.mark.parametrize("terminal", ["succeeded", "uncertain", "failed", "stopped"])
def test_cancel_preserves_all_terminal_results(terminal):
    store = runs.RunStore()
    run_id, _ = observing(store)
    with store._lock:
        state = store._runs[run_id]
        state.status = terminal
        state.verdict = Verdict(status="uncertain", verification_level="none", goal_satisfied=None,
                                reason_codes=["mock_terminal_fixture"], evidence_ids=[])
        store._active_run_id = None
    before = store.get_run(run_id).model_dump()
    for _ in range(2):
        assert store.cancel_run(run_id).status == terminal
        assert store.get_run(run_id).model_dump() == before
    assert_unlocked(store)


def test_http_ack_step_duplicate_and_cancel_without_sensor():
    runs.run_store.reset()
    with TestClient(app) as client:
        created = client.post("/api/runs", json={"user_input": "왼쪽 화력 한 단계 올려줘", "source": "mock", "zone_id": "left"}).json()
        path = f"/api/runs/{created['run_id']}"
        action = client.get(path).json()["instruction"]["action_id"]
        assert client.post(path + "/ack", json={"action_id": action}).json()["status"] == "observing"
        assert client.post(path + "/step").status_code == 422
        assert client.post(path + "/step", json={"action_id": "wrong"}).status_code == 409
        first = client.post(path + "/step", json={"action_id": action})
        assert first.status_code == 200
        result = first.json()
        assert result["status"] == "uncertain"
        assert result["execution_mode"] == "backend_stub"
        assert result["verdict"]["reason_codes"] == ["sensor_adapter_unconfigured"]
        assert result["verdict"]["evidence_ids"] == []
        assert client.post(path + "/step", json={"action_id": action}).json() == result
        assert client.post(path + "/cancel").json()["status"] == "uncertain"
        assert client.get(path).json() == result
        assert client.post("/api/runs", json={"user_input": "화력 올려줘", "source": "mock", "zone_id": "right"}).status_code == 201
    runs.run_store.reset()

def test_recovery_failure_is_bounded_and_releases_active_lock():
    class FailingRecovery(AgentOrchestrator):
        async def execute_observation_and_verification(self, **kwargs):
            return Verdict(status="mismatch", verification_level="none", goal_satisfied=False,
                           reason_codes=["no_output_change"], evidence_ids=[]), "Mock only"

        def handle_recovery(self, **kwargs):
            raise RuntimeError("private recovery diagnostic")
    store = runs.RunStore()
    run_id, action = observing(store)
    result = asyncio.run(store.execute_observation_step(run_id, FailingRecovery(), action))
    assert result.status == "uncertain"
    assert result.verdict.reason_codes == ["observation_processing_error"]
    assert "private recovery diagnostic" not in result.model_dump_json()
    assert_unlocked(store)


def test_real_mock_success_is_preserved_after_cancel():
    from app.adapters.mock_setup import build_mock_adapter
    store = runs.RunStore()
    run_id, action = observing(store)
    worker = AgentOrchestrator(adapter=build_mock_adapter("normal", zone_id="right"))
    result = asyncio.run(store.execute_observation_step(run_id, worker, action))
    assert result.status == "succeeded"  # synthetic Mock evidence, not physical success
    before = store.get_run(run_id).model_dump()
    assert store.cancel_run(run_id).status == "succeeded"
    assert store.get_run(run_id).model_dump() == before
    assert_unlocked(store)
