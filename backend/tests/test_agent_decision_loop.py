"""Model decisions are exercised through real-mode API; fake provider, real safety code."""
import asyncio
import json
import threading
import time
import pytest
from fastapi.testclient import TestClient
from app.api.runs import run_store
from app.config import settings
from app.main import app
from app.llm.base import LlmResponse,ToolCall,LlmUsage
from app.llm.openai_provider import LlmProviderError


class DecisionProvider:
    def __init__(self,actions,hold=None):
        self.actions=list(actions);self.messages=[];self.hold=hold;self.entered=threading.Event()
    async def generate(self,messages,tools=None):
        self.messages.append(json.loads(json.dumps(messages)))
        usage=LlmUsage(prompt_tokens=10,completion_tokens=5,total_tokens=15)
        if 'TOUCHBACK_OBSERVATION_AGENT' not in messages[0]['content']:
            return LlmResponse(text='오른쪽 화구의 + 버튼을 한 번 눌러 주세요.',usage=usage,model='offline-decision-test')
        if self.hold:
            self.entered.set();await asyncio.sleep(self.hold)
        action=self.actions.pop(0) if self.actions else 'stop'
        args={}
        if action not in ('inspect_observation','assess_retry_safety'):
            if action=='free_text':return LlmResponse(text='성공했습니다',usage=usage)
            ids=[]
            for msg in reversed(messages):
                if msg['role']=='tool':
                    data=json.loads(msg['content']).get('data',{})
                    ids=data.get('evidence_ids',data.get('verdict',{}).get('evidence_ids',[]))
                    if ids:break
            args={'action':action,'evidence_ids':ids}
            if action=='forged_evidence':args={'action':'complete','evidence_ids':['not-an-observation']}
            action='choose_next_action'
        return LlmResponse(tool_calls=[ToolCall(id=f't-{len(self.messages)}',name=action,arguments=args)],usage=usage,model='offline-decision-test')


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    run_store.reset()
    monkeypatch.setattr(settings,'execution_mode','real_llm')
    monkeypatch.setattr(settings,'llm_provider','anthropic')
    monkeypatch.setattr(settings,'anthropic_api_key','fake-offline-key')
    monkeypatch.setattr(settings,'demo_enabled',True)
    monkeypatch.setattr(settings,'mock_scenario','normal')
    run_store.virtual_demo=True
    yield
    run_store.reset()


def client():return TestClient(app,base_url='http://127.0.0.1',client=('127.0.0.1',53000))
def begin(c):
    result=c.post('/api/runs',json={'user_input':'오른쪽 화력 한 단계 올려줘','zone_id':'right','source':'mock'}).json()
    run=c.get('/api/runs/'+result['run_id']).json()
    assert run['status']=='awaiting_user',run
    return run

def press(c,run,zone='right',op='increase'):
    action=run['instruction']['action_id'];rid=run['run_id']
    assert c.post('/api/demo/runs/'+rid+'/press',json={'action_id':action,'zone_id':zone,'operation':op}).status_code==200
    return c.post('/api/runs/'+rid+'/step',json={'action_id':action}).json()


@pytest.mark.parametrize('choice,status',[('complete','succeeded'),('stop','uncertain')])
def test_same_normal_observation_model_can_complete_or_stop(monkeypatch,choice,status):
    provider=DecisionProvider(['inspect_observation',choice])
    monkeypatch.setattr('app.api.runs.get_llm_provider',lambda **_:provider)
    with client() as c:
        ended=press(c,begin(c))
        assert ended['status']==status
        assert run_store.active_run_id() is None
        assert any('Agent 다음 행동 선택: '+choice in e['message'] for e in ended['events'])
        before=len(provider.messages)
        assert c.post('/api/runs/'+ended['run_id']+'/step',json={'action_id':ended['simulation']['last_press']['action_id']}).status_code==200
        assert len(provider.messages)==before
        assert c.post('/api/runs/'+ended['run_id']+'/cancel').json()['status']==status
    sent=json.dumps(provider.messages,ensure_ascii=False)
    for forbidden in ('"levels"','"last_press"','"scenario"','fake-offline-key'):
        assert forbidden not in sent


@pytest.mark.parametrize('choice,has_correction',[('offer_correction',True),('stop',False)])
def test_other_zone_proposal_is_model_selected_and_never_auto_operates(monkeypatch,choice,has_correction):
    p=DecisionProvider(['inspect_observation',choice]);monkeypatch.setattr('app.api.runs.get_llm_provider',lambda **_:p)
    with client() as c:
        ended=press(c,begin(c),'left')
        assert ended['status']=='uncertain'
        assert bool(ended['diagnosis']['correction'])==has_correction
        assert ended['simulation']['levels']=={'left':6,'right':5}
        assert run_store.active_run_id() is None


@pytest.mark.parametrize('actions,expected',[(['inspect_observation','assess_retry_safety','retry'],'awaiting_user'),
    (['inspect_observation','stop'],'uncertain'),(['inspect_observation','retry','stop'],'uncertain')])
def test_no_change_model_selects_retry_review_or_stop(monkeypatch,actions,expected):
    monkeypatch.setattr(settings,'mock_scenario','recovery')
    p=DecisionProvider(actions);monkeypatch.setattr('app.api.runs.get_llm_provider',lambda **_:p)
    with client() as c:
        first=begin(c);ended=press(c,first)
        assert ended['status']==expected,ended
        if expected=='awaiting_user':
            assert ended['instruction']['action_id']!=first['instruction']['action_id']
            assert sum(e['kind']=='recovery' for e in ended['events'])==1
            p.actions=['inspect_observation','complete']
            assert press(c,ended)['status']=='succeeded'
        if actions==['inspect_observation','retry','stop']:
            assert any('안전 차단' in e['message'] for e in ended['events'])


@pytest.mark.parametrize('scenario',['sensor_error','missing'])
def test_invalid_evidence_cannot_become_success_or_retry(monkeypatch,scenario):
    monkeypatch.setattr(settings,'mock_scenario',scenario)
    p=DecisionProvider(['inspect_observation','complete','retry','stop']);monkeypatch.setattr('app.api.runs.get_llm_provider',lambda **_:p)
    with client() as c:
        ended=press(c,begin(c));assert ended['status']=='uncertain'
        assert not ended['diagnosis']['correction']
        assert not any(e['kind']=='recovery' for e in ended['events'])


@pytest.mark.parametrize('actions',[
    ['inspect_observation']*4,['free_text'],['inspect_observation','forged_evidence','forged_evidence','forged_evidence']])
def test_exhaustion_free_text_and_forged_ids_fail_closed(monkeypatch,actions):
    p=DecisionProvider(actions);monkeypatch.setattr('app.api.runs.get_llm_provider',lambda **_:p)
    with client() as c:
        ended=press(c,begin(c));assert ended['status']=='failed' and ended['instruction'] is None
        assert run_store.active_run_id() is None


def test_retry_limit_cannot_be_overridden_by_model(monkeypatch):
    monkeypatch.setattr(settings,'mock_scenario','recovery_exhausted')
    p=DecisionProvider(['inspect_observation','assess_retry_safety','retry']);monkeypatch.setattr('app.api.runs.get_llm_provider',lambda **_:p)
    with client() as c:
        second=press(c,begin(c));assert second['status']=='awaiting_user'
        p.actions=['inspect_observation','assess_retry_safety','retry','stop']
        ended=press(c,second);assert ended['status']=='uncertain'
        assert sum(e['kind']=='recovery' for e in ended['events'])==1


def test_cancel_during_model_wait_preserves_terminal_and_records_usage(monkeypatch):
    p=DecisionProvider(['inspect_observation','complete'],hold=.15)
    monkeypatch.setattr('app.api.runs.get_llm_provider',lambda **_:p)
    with client() as c:
        run=begin(c);rid=run['run_id'];action=run['instruction']['action_id']
        c.post('/api/demo/runs/'+rid+'/press',json={'action_id':action,'zone_id':'right','operation':'increase'})
        results=[]
        worker=threading.Thread(target=lambda:results.append(c.post('/api/runs/'+rid+'/step',json={'action_id':action}).json()))
        worker.start();assert p.entered.wait(2)
        assert c.post('/api/runs/'+rid+'/cancel').json()['status']=='stopped'
        next_run=begin(c)
        worker.join(3);assert not worker.is_alive()
        ended=c.get('/api/runs/'+rid).json()
        assert ended['status']=='stopped' and results[0]['status']=='stopped'
        assert not any('Agent 다음 행동 선택' in e['message'] for e in ended['events'])
        assert any('tokens: 15' in e['message'] for e in ended['events'])
        assert run_store.active_run_id()==next_run['run_id']


def test_timeout_during_model_wait_releases_lock_without_late_result(monkeypatch):
    p=DecisionProvider(['inspect_observation','complete'],hold=.2)
    monkeypatch.setattr('app.api.runs.get_llm_provider',lambda **_:p)
    monkeypatch.setattr('app.api.runs.TIMEOUT_OBSERVING_SECONDS',.05)
    with client() as c:
        ended=press(c,begin(c))
        assert ended['status']=='uncertain' and run_store.active_run_id() is None
        rid=ended['run_id']
        time.sleep(.25)
        later=c.get('/api/runs/'+rid).json()
        assert later['status']=='uncertain'
        assert 'observation_timeout' in later['verdict']['reason_codes']
        assert not any('Agent 다음 행동 선택' in e['message'] for e in later['events'])
        assert any('tokens: 15' in e['message'] for e in later['events'])
        assert begin(c)['status']=='awaiting_user'


def test_repeated_safety_assessment_collects_only_once(monkeypatch):
    monkeypatch.setattr(settings,'mock_scenario','recovery')
    p=DecisionProvider(['inspect_observation','assess_retry_safety','assess_retry_safety','retry'])
    monkeypatch.setattr('app.api.runs.get_llm_provider',lambda **_:p)
    from app.agent.orchestrator import AgentOrchestrator
    calls=[];original=AgentOrchestrator.handle_recovery
    def wrapped(self,**kwargs):
        calls.append(1);return original(self,**kwargs)
    monkeypatch.setattr(AgentOrchestrator,'handle_recovery',wrapped)
    with client() as c:
        assert press(c,begin(c))['status']=='awaiting_user'
    assert len(calls)==1


def test_model_must_inspect_before_any_action(monkeypatch):
    p=DecisionProvider(['complete','stop','inspect_observation','stop'])
    monkeypatch.setattr('app.api.runs.get_llm_provider',lambda **_:p)
    with client() as c:
        ended=press(c,begin(c));assert ended['status']=='uncertain'
        assert sum('안전 차단' in e['message'] for e in ended['events'])==2


@pytest.mark.parametrize('actions,expected_count',[
    (['inspect_observation','free_text'],1),(['inspect_observation']*4,4)])
def test_performed_tool_facts_survive_failed_model_loop_once(monkeypatch,actions,expected_count):
    p=DecisionProvider(actions);monkeypatch.setattr('app.api.runs.get_llm_provider',lambda **_:p)
    with client() as c:
        ended=press(c,begin(c));assert ended['status']=='failed'
        facts=[e for e in ended['events'] if e['kind']=='tool_call' and '관측 Agent Tool' in e['message']]
        assert len(facts)==expected_count
        rid=ended['run_id'];action=ended['simulation']['last_press']['action_id']
        repeated=c.post('/api/runs/'+rid+'/step',json={'action_id':action}).json()
        assert repeated['events']==ended['events']
        assert all(e['run_id']==rid and e['action_id']==action for e in facts)
        assert not any('Agent 다음 행동 선택' in e['message'] for e in ended['events'])


def test_performed_inspect_survives_subsequent_provider_failure(monkeypatch):
    class Failure(DecisionProvider):
        async def generate(self,messages,tools=None):
            if messages[-1]['role']=='tool':raise LlmProviderError('offline injected provider failure')
            return await super().generate(messages,tools)
    p=Failure(['inspect_observation']);monkeypatch.setattr('app.api.runs.get_llm_provider',lambda **_:p)
    with client() as c:
        ended=press(c,begin(c));assert ended['status']=='failed'
        assert sum('관측 Agent Tool' in e['message'] for e in ended['events'])==1


@pytest.mark.parametrize('cancel',[False,True])
def test_tool_facts_before_cancel_or_timeout_do_not_publish_a_decision(monkeypatch,cancel):
    class HoldAfterInspect(DecisionProvider):
        async def generate(self,messages,tools=None):
            if messages[-1]['role']=='tool':
                self.entered.set();await asyncio.sleep(.2)
            return await super().generate(messages,tools)
    p=HoldAfterInspect(['inspect_observation','complete'])
    monkeypatch.setattr('app.api.runs.get_llm_provider',lambda **_:p)
    if not cancel:monkeypatch.setattr('app.api.runs.TIMEOUT_OBSERVING_SECONDS',.06)
    with client() as c:
        run=begin(c);rid=run['run_id'];action=run['instruction']['action_id']
        c.post('/api/demo/runs/'+rid+'/press',json={'action_id':action,'zone_id':'right','operation':'increase'})
        results=[];worker=threading.Thread(target=lambda:results.append(c.post('/api/runs/'+rid+'/step',json={'action_id':action}).json()))
        worker.start();assert p.entered.wait(2)
        if cancel:c.post('/api/runs/'+rid+'/cancel')
        worker.join(3);assert not worker.is_alive()
        time.sleep(.25)
        next_run=begin(c)
        ended=c.get('/api/runs/'+rid).json()
        assert ended['status']==('stopped' if cancel else 'uncertain')
        assert sum('관측 Agent Tool' in e['message'] for e in ended['events'])==1
        assert not any('Agent 다음 행동 선택' in e['message'] for e in ended['events'])
        assert len([e for e in ended['events'] if '복구 토큰 메트릭' in e['message']])==1
        assert run_store.active_run_id()==next_run['run_id']
