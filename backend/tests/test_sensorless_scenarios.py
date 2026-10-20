"""Actual HTTP lifecycle with Mock observations; no physical or external LLM calls."""
from unittest.mock import patch
import json
import pytest
from fastapi.testclient import TestClient
from app.api.runs import run_store
from app.config import settings,Settings
from app.main import app
from app.llm.mock_provider import MockLlmProvider


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    run_store.reset()
    monkeypatch.setattr(settings,"execution_mode","mock_llm")
    monkeypatch.setattr(settings,"demo_enabled",True)
    monkeypatch.setattr(settings,"mock_scenario","normal")
    yield
    run_store.reset()


@pytest.mark.parametrize("zone",["right","left"])
@pytest.mark.parametrize("scenario,expected,retries",[("normal","succeeded",0),("recovery","succeeded",1),
    ("sensor_error","uncertain",0),("missing","uncertain",0),("delayed","succeeded",0),
    ("no_change","uncertain",0),("duplicate","succeeded",0),("recovery_exhausted","uncertain",1)])
def test_sensorless_api_scenarios_are_observation_driven(zone,scenario,expected,retries):
    with TestClient(app,base_url="http://127.0.0.1",client=("127.0.0.1",50000)) as client:
        assert client.post('/api/demo/scenario',json={"scenario":scenario}).status_code==200
        created=client.post('/api/runs',json={"user_input":"화력 한 단계 올려줘","source":"mock","zone_id":zone}).json()
        path='/api/runs/'+created['run_id']; run=client.get(path).json();assert run['status']=='awaiting_user'
        seen=[]
        for _ in range(2):
            action=run['instruction']['action_id'];seen.append(action)
            assert client.post(path+'/ack',json={"action_id":action}).status_code==200
            run=client.post(path+'/step',json={"action_id":action}).json()
            if run['status']!='awaiting_user':break
        assert run['status']==expected and len(seen)==1+retries and len(set(seen))==len(seen)
        assert run['source']=='mock' and run['execution_mode']=='mock_llm'
        assert not any('scenario' in e['message'] for e in run['events'])
        before=run.copy()
        assert client.post(path+'/step',json={"action_id":seen[-1]}).json()==before
        assert client.post('/api/runs',json={"user_input":"화력 올려줘","source":"mock","zone_id":zone}).status_code==201


def test_operator_selection_cannot_change_an_active_run():
    with TestClient(app,base_url="http://127.0.0.1",client=("127.0.0.1",50000)) as client:
        created=client.post('/api/runs',json={"user_input":"화력 올려줘","source":"mock","zone_id":"right"}).json()
        assert client.post('/api/demo/scenario',json={"scenario":"sensor_error"}).status_code==409
        assert settings.mock_scenario=='normal'
        client.post('/api/runs/'+created['run_id']+'/cancel')
        assert client.post('/api/demo/scenario',json={"scenario":"sensor_error"}).status_code==200


@pytest.mark.parametrize("host",["testclient","203.0.113.1"])
def test_remote_controls_not_exposed(host):
    with TestClient(app,base_url="http://127.0.0.1",client=(host,50000)) as client:
        assert client.get('/api/demo').json()=={"enabled":False}
        assert client.post('/api/demo/scenario',json={"scenario":"normal"}).status_code==403


def test_disabled_stub_cross_origin_and_invalid_scenario_are_rejected(monkeypatch):
    with TestClient(app,base_url="http://127.0.0.1",client=("127.0.0.1",50000)) as client:
        monkeypatch.setattr(settings,"demo_enabled",False)
        assert client.get('/api/demo').json()["enabled"] is False
        assert client.get('/api/demo').json()["manageable"] is True
        monkeypatch.setattr(settings,"demo_enabled",True)
        monkeypatch.setattr(settings,"execution_mode","backend_stub")
        assert client.post('/api/demo/scenario',json={"scenario":"normal"}).status_code==403
        monkeypatch.setattr(settings,"execution_mode","mock_llm")
        assert client.post('/api/demo/scenario',json={"scenario":"unknown"}).status_code==422
        assert client.post('/api/demo/scenario',json={"scenario":"normal"},headers={"origin":"https://untrusted.example"}).status_code==403


def test_scenario_label_is_not_in_model_messages_or_tools():
    provider=MockLlmProvider();seen=[]
    original=provider.generate
    async def capture(messages,tools=None):
        seen.append(json.dumps({"messages":messages,"tools":tools},ensure_ascii=False))
        return await original(messages,tools)
    provider.generate=capture
    with patch('app.api.runs.get_llm_provider',return_value=provider),TestClient(app,base_url="http://127.0.0.1",client=("127.0.0.1",50000)) as client:
        client.post('/api/demo/scenario',json={"scenario":"recovery_exhausted"})
        assert client.post('/api/runs',json={"user_input":"화력 올려줘","source":"mock","zone_id":"right"}).status_code==201
    assert seen and all('recovery_exhausted' not in text and 'scenario' not in text for text in seen)

def test_local_address_with_untrusted_host_header_cannot_enable_operator_controls():
    with TestClient(app,base_url="http://untrusted.example",client=("127.0.0.1",50000)) as client:
        assert client.get('/api/demo').json()=={"enabled":False}
        assert client.post('/api/demo/scenario',json={"scenario":"normal"}).status_code==403

def test_demo_settings_environment_overrides_local_file_without_changing_model_mode(tmp_path,monkeypatch):
    import app.config as config
    (tmp_path/'.env').write_text('TOUCHBACK_DEMO_ENABLED=true\nTOUCHBACK_MOCK_SCENARIO=recovery\n',encoding='utf-8')
    monkeypatch.setattr(config,'find_repo_root',lambda:tmp_path)
    monkeypatch.setenv('TOUCHBACK_DEMO_ENABLED','false')
    monkeypatch.delenv('TOUCHBACK_MOCK_SCENARIO',raising=False)
    monkeypatch.setenv('TOUCHBACK_EXECUTION_MODE','backend_stub')
    loaded=Settings.load()
    assert loaded.demo_enabled is False and loaded.mock_scenario=='recovery'
    assert loaded.execution_mode=='backend_stub'


def test_invalid_demo_configuration_fails_explicitly(tmp_path,monkeypatch):
    import app.config as config
    monkeypatch.setattr(config,'find_repo_root',lambda:tmp_path)
    monkeypatch.setenv('TOUCHBACK_DEMO_ENABLED','maybe')
    with pytest.raises(ValueError):Settings.load()
    monkeypatch.setenv('TOUCHBACK_DEMO_ENABLED','true')
    monkeypatch.setenv('TOUCHBACK_MOCK_SCENARIO','unknown')
    with pytest.raises(ValueError):Settings.load()
