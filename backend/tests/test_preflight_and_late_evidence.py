"""Public API regressions for calibration preflight and delayed original-action evidence."""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.adapters.mock import MockAdapter, ScheduledObservation, VirtualClock
from app.adapters.mock_setup import build_mock_adapter
from app.api.runs import run_store
from app.config import settings
from app.llm.base import LlmResponse
from app.llm.mock_provider import MockLlmProvider
from app.main import app
from tests.test_misoperation_recovery import build_multi_step_mock_adapter


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    run_store.reset()
    monkeypatch.setattr(settings,'execution_mode','mock_llm')
    yield
    run_store.reset()


def corrupt(baseline, fault):
    result=baseline.model_copy(deep=True)
    field=result.device_observation.field_summary
    if fault=='id':result.device_observation.calibration_id='unregistered-profile'
    elif fault=='unit':field.unit='millivolt'
    elif fault=='range':field.rms=999.0
    elif fault=='samples':field.sample_count=1
    elif fault=='method':field.method='different-method'
    elif fault=='rate':field.sample_rate_hz=2000
    return result


def start(client,zone):
    created=client.post('/api/runs',json={'user_input':'화력 한 단계 올려줘','source':'mock','zone_id':zone})
    assert created.status_code==201
    path='/api/runs/'+created.json()['run_id']
    return path,client.get(path).json()


@pytest.mark.parametrize('zone',['right','left'])
@pytest.mark.parametrize('fault',['none','id','unit','range','samples','method','rate'])
def test_invalid_baseline_is_blocked_before_first_instruction(zone,fault):
    adapter=build_mock_adapter('normal',zone_id=zone)
    baseline=corrupt(adapter.read_baseline(zone),fault)
    provider=MockLlmProvider()
    label='오른쪽' if zone=='right' else '왼쪽'
    provider.queue_text(f'{label} 화구의 + 버튼을 한 번 눌러 주세요.')
    with patch.object(adapter,'read_baseline',return_value=baseline),patch('app.api.runs.resolve_adapter_for_run',return_value=adapter),patch('app.api.runs.get_llm_provider',return_value=provider),TestClient(app) as client:
        _,run=start(client,zone)
    if fault=='none':assert run['status']=='awaiting_user'
    else:
        assert run['status']=='uncertain' and run['instruction'] is None
        assert not any(e['kind']=='instruction' for e in run['events'])
        assert run_store._active_run_id is None


@pytest.mark.parametrize('fault',['id','unit','range','samples','method','rate'])
def test_recovery_preflight_rejects_baseline_changed_during_model_wait(fault):
    adapter=build_multi_step_mock_adapter()
    class Provider(MockLlmProvider):
        async def generate(self,messages,tools=None):
            if tools is None:
                baseline=corrupt(adapter.read_baseline('right'),fault)
                adapter.read_baseline=lambda zone:baseline
                return LlmResponse(text='오른쪽 화구의 + 버튼을 다시 한 번 눌러 주세요.')
            return LlmResponse(text='오른쪽 화구의 + 버튼을 한 번 눌러 주세요.')
    with patch('app.api.runs.resolve_adapter_for_run',return_value=adapter),patch('app.api.runs.get_llm_provider',return_value=Provider()),TestClient(app) as client:
        path,run=start(client,'right');action={'action_id':run['instruction']['action_id']}
        client.post(path+'/ack',json=action);final=client.post(path+'/step',json=action).json()
        assert final['status']=='uncertain' and final['instruction'] is None
        assert not any(e['kind']=='recovery' for e in final['events'])
        assert run_store._active_run_id is None


@pytest.mark.parametrize('zone',['right','left'])
@pytest.mark.parametrize('condition',['exact','output_only','wrong_step','bad_quality','outside_window'])
def test_late_after_no_change_uses_original_baseline_without_second_instruction(zone,condition):
    source=build_multi_step_mock_adapter()
    schedule=source._pending[:6]
    for item in schedule:item.observation.zone_id=zone
    late=source._pending[6].observation.model_copy(deep=True)
    late.zone_id=zone;late.window_start_ms=2000;late.window_end_ms=2500
    late.field_summary.rms={'exact':120.0,'output_only':110.0,'wrong_step':140.0}.get(condition,120.0)
    if condition=='bad_quality':late.quality.status='invalid';late.quality.reasons=['synthetic-fault']
    if condition=='outside_window':late.window_start_ms=3300;late.window_end_ms=3800
    schedule.append(ScheduledObservation(4000,late))
    adapter=MockAdapter(schedule,VirtualClock(now_ms=500))
    provider=MockLlmProvider();label='오른쪽' if zone=='right' else '왼쪽'
    provider.queue_text(f'{label} 화구의 + 버튼을 한 번 눌러 주세요.')
    with patch('app.api.runs.resolve_adapter_for_run',return_value=adapter),patch('app.api.runs.get_llm_provider',return_value=provider),TestClient(app) as client:
        path,run=start(client,zone);action={'action_id':run['instruction']['action_id']}
        client.post(path+'/ack',json=action);final=client.post(path+'/step',json=action).json()
        assert final['status']==('succeeded' if condition=='exact' else 'uncertain')
        assert not any(e['kind']=='recovery' for e in final['events'])
        if condition=='exact':
            assert final['verdict']['verification_level']=='exact_step'
            assert any(e['kind']=='verdict' and e['evidence_ids']==final['verdict']['evidence_ids'] for e in final['events'])
        assert client.post(path+'/step',json=action).json()==final
        assert run_store._active_run_id is None
