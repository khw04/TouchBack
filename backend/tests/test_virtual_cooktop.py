from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import json
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.api.runs import run_store
from app.config import settings
from app.llm.mock_provider import MockLlmProvider

@pytest.fixture(autouse=True)
def clean(monkeypatch):
    run_store.reset()
    monkeypatch.setattr(settings,"execution_mode","backend_stub")
    monkeypatch.setattr(settings,"demo_enabled",False)
    monkeypatch.setattr(settings,"mock_scenario","normal")
    monkeypatch.setattr(settings,"openai_api_key","")
    yield
    run_store.reset()

def client():return TestClient(app,base_url="http://127.0.0.1",client=("127.0.0.1",50000))
def start(c,scenario='normal',zone='right'):
    assert c.post('/api/demo/settings',json={'execution_mode':'mock_llm','scenario':scenario}).status_code==200
    created=c.post('/api/runs',json={'user_input':'화력 한 단계 올려줘','source':'mock','zone_id':zone}).json()
    return '/api/runs/'+created['run_id']
def press(c,path,run,zone='right',op='increase'):
    return c.post('/api/demo/runs/'+run['run_id']+'/press',json={'action_id':run['instruction']['action_id'],'zone_id':zone,'operation':op})

@pytest.mark.parametrize('zone',['right','left'])
@pytest.mark.parametrize('scenario,status,retries',[
 ('normal','succeeded',0),('recovery','succeeded',1),('sensor_error','uncertain',0),('missing','uncertain',0),
 ('delayed','succeeded',0),('no_change','uncertain',0),('duplicate','succeeded',0),('recovery_exhausted','uncertain',1)])
def test_virtual_press_drives_existing_verifier_and_recovery(zone,scenario,status,retries):
    with client() as c:
        path=start(c,scenario,zone);run=c.get(path).json();seen=[]
        assert run['simulation']['levels']=={'left':5,'right':5}
        for _ in range(2):
            action=run['instruction']['action_id'];seen.append(action)
            r=press(c,path,run,zone);assert r.status_code==200
            assert r.json()['status']=='observing'
            run=c.post(path+'/step',json={'action_id':action}).json()
            if run['status']!='awaiting_user':break
        assert run['status']==status,run
        assert len(seen)==1+retries
        assert c.post(path+'/step',json={'action_id':seen[-1]}).json()==run
        if status=='succeeded':
            assert run['verdict']['evidence_ids'] and run['simulation']['levels'][zone]==6
        assert c.post('/api/runs',json={'user_input':'화력 올려줘','source':'mock','zone_id':zone}).status_code==201

@pytest.mark.parametrize('zone,op',[('left','increase'),('right','decrease')])
def test_wrong_control_is_not_success(zone,op):
    with client() as c:
        path=start(c);run=c.get(path).json();action=run['instruction']['action_id']
        r=press(c,path,run,zone,op);assert r.status_code==200
        result=c.post(path+'/step',json={'action_id':action}).json()
        assert result['status']!='succeeded'
        assert result['verdict']['goal_satisfied'] is not True

def test_same_press_is_idempotent_conflicting_press_is_rejected_and_ack_requires_press():
    with client() as c:
        path=start(c);run=c.get(path).json();action=run['instruction']['action_id']
        assert c.post(path+'/ack',json={'action_id':action}).status_code==409
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:press(c,path,run),range(2)))
        assert all(r.status_code==200 for r in results)
        assert c.get(path).json()['simulation']['levels']['right']==6
        assert press(c,path,run,'left').status_code==409
        c.post(path+'/cancel');before=c.get(path).json()
        assert press(c,path,run).json()==before
        assert c.get(path).json()==before

def test_cancelled_unpressed_action_and_remote_press_cannot_mutate_levels():
    with client() as c:
        path=start(c);run=c.get(path).json()
        assert press(c,path,run,op='power').status_code==422
        assert c.post('/api/demo/runs/'+run['run_id']+'/press',json={'action_id':run['instruction']['action_id'],'zone_id':'right','operation':'increase'},headers={'origin':'https://other.example'}).status_code==403
        c.post(path+'/cancel')
        assert press(c,path,run).status_code==409
        assert c.get(path).json()['simulation']['levels']=={'left':5,'right':5}

def test_internal_simulator_state_is_not_passed_to_model():
    provider=MockLlmProvider();original=provider.generate;messages=[]
    async def capture(history,tools=None):
        messages.append(json.dumps({'messages':history,'tools':tools},ensure_ascii=False))
        return await original(history,tools)
    provider.generate=capture
    with patch('app.api.runs.get_llm_provider',return_value=provider),client() as c:
        path=start(c,'recovery_exhausted');run=c.get(path).json();action=run['instruction']['action_id']
        press(c,path,run);c.post(path+'/step',json={'action_id':action})
    assert messages
    assert all('recovery_exhausted' not in m and '"levels"' not in m and '"simulation"' not in m for m in messages)


def test_displayed_signals_are_consumed_evidence_with_the_original_baseline():
    with client() as c:
        path=start(c);run=c.get(path).json();action=run['instruction']['action_id']
        assert run['simulation']['observations']==[]
        r=press(c,path,run).json()
        assert r['simulation']['observations']==[]
        baseline=r['simulation']['baselines'][action]
        assert baseline['device_observation']['field_summary']['rms']==100
        result=c.post(path+'/step',json={'action_id':action}).json()
        receipts=result['simulation']['observations']
        assert len(receipts)==2
        assert {item["device_observation"]["zone_id"] for item in receipts}=={"left","right"}
        assert receipts[0]['observation_id'] in result['verdict']['evidence_ids']
        assert receipts[0]['action_id']==action and receipts[0]['run_id']==run['run_id']
        assert receipts[0]['device_observation']['field_summary']['rms']==120
        assert result['simulation']['baselines'][action]==baseline


def test_scripted_rehearsal_rejects_interactive_mode_before_ack_calls():
    from app.rehearsal.runner import _check_metadata,RehearsalError
    with pytest.raises(RehearsalError,match='interactive'):
        _check_metadata({'enabled':True,'virtual_cooktop':True})
