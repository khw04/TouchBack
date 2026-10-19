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
    monkeypatch.setattr(settings,"mock_scenario","normal")
    monkeypatch.setattr(settings,"openai_api_key","")
    yield
    run_store.reset()

def client():
    return TestClient(app,base_url="http://127.0.0.1",client=("127.0.0.1",50000))

def test_explicit_screen_setup_without_environment_changes():
    with client() as c:
        info=c.get('/api/demo').json()
        assert not info['enabled'] and info['manageable'] and not info['api_key_configured']
        assert 'openai_api_key' not in info
        r=c.post('/api/demo/settings',json={'execution_mode':'mock_llm','scenario':'recovery'})
        assert r.status_code==200 and r.json()['enabled'] and r.json()['scenario']=='recovery'
        assert settings.execution_mode=='mock_llm'

def test_real_mode_without_key_is_rejected_without_mutation():
    with client() as c:
        assert c.post('/api/demo/settings',json={'execution_mode':'real_llm'}).status_code==422
        assert settings.execution_mode=='backend_stub' and settings.demo_enabled is False

@pytest.mark.parametrize('host,origin',[('203.0.113.1',None),('127.0.0.1','https://other.example')])
def test_setup_rejects_remote_and_cross_origin(host,origin):
    with TestClient(app,base_url="http://127.0.0.1",client=(host,50000)) as c:
        assert c.post('/api/demo/settings',json={'execution_mode':'mock_llm'},headers={'origin':origin} if origin else {}).status_code==403
        assert settings.execution_mode=='backend_stub'

def test_setup_cannot_change_active_mode_or_scenario():
    with client() as c:
        c.post('/api/demo/settings',json={'execution_mode':'mock_llm'})
        created=c.post('/api/runs',json={'user_input':'오른쪽 화력 올려줘','source':'mock','zone_id':'right'}).json()
        assert c.post('/api/demo/settings',json={'execution_mode':'mock_llm','scenario':'sensor_error'}).status_code==409
        assert settings.mock_scenario=='normal'
        c.post('/api/runs/'+created['run_id']+'/cancel')
        assert c.post('/api/demo/settings',json={'execution_mode':'mock_llm','scenario':'sensor_error'}).status_code==200
