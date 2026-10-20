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

def begin(c,text="오른쪽 화력 한 단계 올려줘"):
    r=c.post("/api/runs",json={"user_input":text,"zone_id":None,"source":"mock"})
    assert r.status_code==201
    run=c.get("/api/runs/"+r.json()["run_id"]).json()
    assert run["status"]=="awaiting_user",run
    return run

def apply(c,run,zone,operation):
    rid=run["run_id"];aid=run["instruction"]["action_id"]
    c.post("/api/demo/runs/"+rid+"/press",json={"action_id":aid,"zone_id":zone,"operation":operation})
    return c.post("/api/runs/"+rid+"/step",json={"action_id":aid}).json()


@pytest.mark.parametrize("zone,op,scenario,code,status",[
    ("right","increase","normal","goal_verified","succeeded"),
    ("left","increase","normal","other_zone_changed","uncertain"),
    ("left","decrease","normal","other_zone_changed","uncertain"),
    ("right","decrease","normal","opposite_direction","uncertain"),
    ("right","increase","double_input","different_step_change","uncertain"),
    ("right","increase","sensor_error","unreliable_observation","uncertain"),
    ("right","increase","missing","unreliable_observation","uncertain"),
])
def test_paired_diagnosis_is_based_on_observed_changes(zone,op,scenario,code,status):
    with client() as c:
        c.post("/api/demo/settings",json={"execution_mode":"mock_llm","scenario":scenario})
        run=begin(c);result=apply(c,run,zone,op)
        assert result["status"]==status,result
        assert result["diagnosis"]["code"]==code
        assert result["diagnosis"]["source"]=="mock"
        if code=="other_zone_changed":
            assert result["diagnosis"]["zones"]["left"]["delta"]==(20 if op=="increase" else -20)
            assert result["diagnosis"]["zones"]["right"]["delta"]==0
        if code=="different_step_change":
            assert "터치 횟수는 확정하지" in result["diagnosis"]["message"]
            assert result["diagnosis"]["correction"] is None


def test_user_confirmed_correction_decrease_then_original_goal_are_separate_verified_runs():
    with client() as c:
        c.post("/api/demo/settings",json={"execution_mode":"mock_llm","scenario":"normal"})
        wrong=apply(c,begin(c),"left","increase")
        assert wrong["diagnosis"]["correction"]=={"zone_id":"left","direction":"decrease"}
        correction=begin(c,"왼쪽 화력 한 단계 내려줘")
        assert "−" in correction["instruction"]["text"]
        fixed=apply(c,correction,"left","decrease")
        assert fixed["status"]=="succeeded" and fixed["simulation"]["levels"]=={"left":5,"right":5}
        assert apply(c,begin(c),"right","increase")["status"]=="succeeded"


def test_corrupt_other_zone_cannot_hide_behind_a_valid_target_or_device_truth():
    from app.agent.diagnosis import diagnose_pair
    from app.verification.models import ActionContext
    from app.contracts.models import Goal
    from app.adapters.virtual_cooktop import VirtualCooktopAdapter
    a=VirtualCooktopAdapter("normal","right")
    bases={z:a.read_baseline(z) for z in ("left","right")};profiles={z:a.calibration_for_zone(z) for z in bases}
    a.press("act","left","increase",500,3500)
    observed=a.await_zone_pair(run_id="run",action_id="act",zone_id="right",window_start_ms=500,window_end_ms=3500)
    context=ActionContext(run_id="run",action_id="act",window_start_ms=500,window_end_ms=3500,now_ms=a.clock.now_ms)
    a.levels={"left":0,"right":9};a.presses.clear()
    diagnosis,_=diagnose_pair(Goal(zone_id="right",direction="increase",requested_steps=1),bases,profiles,observed,context)
    assert diagnosis["code"]=="other_zone_changed"
    observed[-1].device_observation.quality.status="invalid"
    diagnosis,result=diagnose_pair(Goal(zone_id="right",direction="increase",requested_steps=1),bases,profiles,observed,context)
    assert diagnosis["code"]=="unreliable_observation" and result.status=="uncertain"
