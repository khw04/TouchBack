"""Recover identity without releasing or replacing the single active execution."""
from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient
from app.api.runs import run_store
from app.main import app
from app.config import settings

@pytest.fixture(autouse=True)
def clean(monkeypatch):
    run_store.reset()
    monkeypatch.setattr(settings, "execution_mode", "backend_stub")
    monkeypatch.setattr(settings, "demo_enabled", False)
    monkeypatch.setattr(settings, "openai_api_key", "")
    yield
    run_store.reset()


def local():
    return TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))


def request(text="오른쪽 화력 한 단계 올려줘"):
    return {"user_input": text, "source": "mock", "zone_id": "right"}


@pytest.mark.parametrize("text", ["방귀", "음악 시작해", "화력 두 단계 올려줘", "왼쪽 화력 올려줘"])
def test_clarification_exposes_recovery_identity_without_silently_cancelling(text):
    with local() as c:
        assert c.post("/api/demo/settings", json={"execution_mode":"mock_llm","scenario":"normal"}).status_code == 200
        created=c.post("/api/runs",json=request(text))
        assert created.status_code == 201
        rid=created.json()["run_id"]
        before=c.get("/api/runs/"+rid).json()
        assert before["status"] == "awaiting_clarification" and before["instruction"] is None
        assert c.get("/api/demo").json()["active_run_id"] == rid
        for path,body in [("/api/runs",request()),("/api/demo/settings",{"execution_mode":"mock_llm","scenario":"normal"}),("/api/demo/scenario",{"scenario":"recovery"})]:
            conflict=c.post(path,json=body)
            assert conflict.status_code == 409 and conflict.json()["code"] == "busy"
            assert conflict.json()["active_run_id"] == rid
        assert c.get("/api/runs/"+rid).json() == before
        assert c.post("/api/runs/"+rid+"/cancel").json()["status"] == "stopped"
        assert c.get("/api/demo").json()["active_run_id"] is None
        assert c.post("/api/runs",json=request()).status_code == 201


def test_metadata_expiration_uses_existing_120_second_clarification_limit():
    now=datetime(2026,10,19,tzinfo=timezone.utc)
    run_store.set_clock(lambda:now)
    with local() as c:
        rid=c.post("/api/runs",json=request("방귀")).json()["run_id"]
        now+=timedelta(seconds=119)
        assert c.get("/api/demo").json()["active_run_id"] == rid
        now+=timedelta(seconds=1)
        assert c.get("/api/demo").json()["active_run_id"] is None
        assert c.get("/api/runs/"+rid).json()["status"] == "stopped"
        assert c.post("/api/runs",json=request()).status_code == 201


def test_remote_metadata_does_not_publish_recovery_identity_and_no_active_error_is_unchanged():
    with local() as c:
        rid=c.post("/api/runs",json=request()).json()["run_id"]
        assert c.get("/api/demo").json()["active_run_id"] == rid
    with TestClient(app,base_url="http://example.com",client=("192.0.2.10",50000)) as remote:
        assert remote.get("/api/demo").json() == {"enabled":False}
    with local() as c:
        error=c.get("/api/runs/missing").json()
        assert error["code"] == "unknown_run" and "active_run_id" not in error
        c.post("/api/runs/"+rid+"/cancel")
        assert c.get("/api/demo").json()["active_run_id"] is None
