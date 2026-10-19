from fastapi.testclient import TestClient
import pytest
from app.main import app
from app.api.runs import run_store
from app.config import settings

@pytest.fixture(autouse=True)
def clean(monkeypatch):
    run_store.reset()
    monkeypatch.setattr(settings,"execution_mode","backend_stub")
    monkeypatch.setattr(settings,"demo_enabled",False)
    monkeypatch.setattr(settings,"openai_api_key","")
    yield
    run_store.reset()


def client():return TestClient(app,base_url="http://127.0.0.1",client=("127.0.0.1",50000))

def begin(c,zone="right"):
    created=c.post("/api/runs",json={"user_input":"화력 한 단계 올려줘","zone_id":zone,"source":"mock"})
    assert created.status_code==201
    run=c.get("/api/runs/"+created.json()["run_id"]).json()
    assert run["status"]=="awaiting_user",run
    return run

def apply(c,run,zone="right",operation="increase"):
    rid=run["run_id"];action=run["instruction"]["action_id"]
    assert c.post("/api/demo/runs/"+rid+"/press",json={"action_id":action,"zone_id":zone,"operation":operation}).status_code==200
    return c.post("/api/runs/"+rid+"/step",json={"action_id":action}).json()


def test_sessions_preserve_levels_with_fresh_calibrated_baselines_and_frozen_old_results():
    with client() as c:
        c.post("/api/demo/settings",json={"execution_mode":"mock_llm","scenario":"normal"})
        first=apply(c,begin(c));assert first["status"]=="succeeded"
        second=begin(c);assert second["simulation"]["levels"]=={"left":5,"right":6}
        result=apply(c,second);assert result["status"]=="succeeded"
        base=result["simulation"]["baselines"][second["instruction"]["action_id"]]
        assert base["device_observation"]["field_summary"]["rms"]==120
        assert set(first["verdict"]["evidence_ids"]).isdisjoint(result["verdict"]["evidence_ids"])
        assert c.get("/api/runs/"+first["run_id"]).json()==first
        third=apply(c,begin(c,"left"),"left");assert third["status"]=="succeeded"
        assert third["simulation"]["levels"]=={"left":6,"right":7}


def test_explicit_reset_is_blocked_by_active_run_and_never_changes_old_run():
    with client() as c:
        c.post("/api/demo/settings",json={"execution_mode":"mock_llm","scenario":"normal"})
        run=begin(c)
        blocked=c.post("/api/demo/device/reset");assert blocked.status_code==409 and blocked.json()["active_run_id"]==run["run_id"]
        ended=apply(c,run)
        assert c.post("/api/demo/device/reset",headers={"Origin":"https://elsewhere.invalid"}).status_code==403
        reset=c.post("/api/demo/device/reset");assert reset.status_code==200
        assert reset.json()["virtual_levels"]=={"left":5,"right":5}
        assert c.get("/api/runs/"+run["run_id"]).json()==ended
        assert begin(c)["simulation"]["levels"]=={"left":5,"right":5}


def test_cancel_after_press_preserves_device_change_without_claiming_verified_success():
    with client() as c:
        c.post("/api/demo/settings",json={"execution_mode":"mock_llm","scenario":"normal"})
        run=begin(c);rid=run["run_id"]
        c.post("/api/demo/runs/"+rid+"/press",json={"action_id":run["instruction"]["action_id"],"zone_id":"left","operation":"increase"})
        assert c.post("/api/runs/"+rid+"/cancel").json()["status"]=="stopped"
        assert begin(c)["simulation"]["levels"]=={"left":6,"right":5}
