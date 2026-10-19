import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.api.runs import run_store
from app.config import settings

@pytest.mark.parametrize('fault',['left_tracker','new_required_tool'])
def test_all_required_preflight_failures_block_instruction(monkeypatch,fault):
    from app.agent.orchestrator import AgentOrchestrator
    from app.api import runs
    run_store.reset()
    monkeypatch.setattr(settings,'execution_mode','backend_stub');monkeypatch.setattr(settings,'demo_enabled',False)
    if fault=='left_tracker':
        original=AgentOrchestrator.plan_and_instruct
        async def plan(self,*args,**kwargs):
            result=await original(self,*args,**kwargs)
            self.adapter.tracker.zones['left'].invalidate('sensor_integrity_invalid')
            return result
        monkeypatch.setattr(AgentOrchestrator,'plan_and_instruct',plan)
    else:
        original=runs.required_preflight
        async def checks(*args,**kwargs):
            return await original(*args,**kwargs)+[{'name':'future_required_sensor_check','arguments':{},'origin':'required_preflight','result':{'success':False,'error':'injected required failure'}}]
        monkeypatch.setattr(runs,'required_preflight',checks)
    try:
        with TestClient(app,base_url='http://127.0.0.1',client=('127.0.0.1',50000)) as c:
            c.post('/api/demo/settings',json={'execution_mode':'mock_llm','scenario':'normal'})
            created=c.post('/api/runs',json={'user_input':'오른쪽 화력 올려줘','source':'mock','zone_id':None}).json()
            run=c.get('/api/runs/'+created['run_id']).json()
            assert run['status']=='uncertain' and run['instruction'] is None
            assert not any(e['kind']=='instruction' for e in run['events'])
            assert any(e['kind']=='tool_call' and '실행 실패' in e['message'] for e in run['events'])
            assert run_store.active_run_id() is None
    finally:run_store.reset()
