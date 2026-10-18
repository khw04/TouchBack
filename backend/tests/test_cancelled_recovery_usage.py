"""Late recovery billing is retained without reopening a cancelled/expired run."""
import asyncio
import threading

import pytest

import app.api.runs as runs
from app.config import settings
from app.contracts.models import AckRequest, CreateRunRequest
from app.llm.base import BaseLlmProvider, LlmResponse, LlmUsage
from app.llm.openai_provider import LlmResponseFormatError
from tests.test_misoperation_recovery import build_multi_step_mock_adapter

REQUEST = CreateRunRequest(user_input='화력 한 단계 올려줘', source='mock', zone_id='right')


class DelayedRecoveryProvider(BaseLlmProvider):
    def __init__(self, error):
        self.error = error
        self.started = threading.Event()
        self.release = threading.Event()
        self.recovery_calls = 0

    async def generate(self, messages, tools=None):
        if tools is not None:
            return LlmResponse(text='오른쪽 화구의 + 버튼을 한 번 눌러 주세요.',
                               usage=LlmUsage(total_tokens=10), model='mock-billing')
        self.recovery_calls += 1
        self.started.set()
        assert self.release.wait(3), 'release the mock response'
        usage = LlmUsage(prompt_tokens=45, completion_tokens=18, total_tokens=63)
        if self.error:
            raise LlmResponseFormatError('offline rejected response', usage=usage, model='mock-billing')
        return LlmResponse(text='오른쪽 화구의 + 버튼을 다시 한 번 눌러 주세요.', usage=usage, model='mock-billing')


@pytest.mark.parametrize('error', [False, True], ids=['response', 'format-error-with-usage'])
@pytest.mark.parametrize('end', ['cancel', 'timeout'])
def test_recovery_usage_after_terminal_is_recorded_once_without_state_overwrite(monkeypatch, error, end):
    monkeypatch.setattr(settings, 'execution_mode', 'mock_llm')
    if end == 'timeout':
        monkeypatch.setattr(runs, 'TIMEOUT_OBSERVING_SECONDS', 0.03)
    provider = DelayedRecoveryProvider(error)
    monkeypatch.setattr(runs, 'get_llm_provider', lambda **kwargs: provider)
    monkeypatch.setattr(runs, 'resolve_adapter_for_run', lambda state: build_multi_step_mock_adapter())
    store = runs.RunStore()
    usage_saved = threading.Event()
    original_add = store._add_event
    def capture(state, kind, message, *args, **kwargs):
        original_add(state, kind, message, *args, **kwargs)
        if '토큰 메트릭' in message and 'tokens: 63 ' in message:
            usage_saved.set()
    monkeypatch.setattr(store, '_add_event', capture)
    created = store.create_run(REQUEST)
    action = store.get_run(created.run_id).instruction.action_id
    store.ack_run(created.run_id, AckRequest(action_id=action))

    async def scenario():
        task = asyncio.create_task(store.execute_observation_step(created.run_id, action_id=action))
        try:
            assert await asyncio.to_thread(provider.started.wait, 1)
            # Duplicate polling/step must not issue another model call.
            await store.execute_observation_step(created.run_id, action_id=action)
            if end == 'cancel':
                assert store.cancel_run(created.run_id).status == 'stopped'
            else:
                assert (await task).status == 'uncertain'
            before = store.get_run(created.run_id).model_dump()
            next_run = store.create_run(REQUEST)
            next_before = store.get_run(next_run.run_id).model_dump()
            provider.release.set()
            await task
            assert await asyncio.to_thread(usage_saved.wait, 1)
            after = store.get_run(created.run_id).model_dump()
            assert {k:v for k,v in after.items() if k != 'events'} == {k:v for k,v in before.items() if k != 'events'}
            assert after['events'][:-1] == before['events']
            assert after['events'][-1]['kind'] == 'plan'
            assert after['events'][-1]['action_id'] == action
            metrics = [e['message'] for e in after['events'] if '토큰 메트릭' in e['message']]
            assert len(metrics) == 2 and 'tokens: 10 ' in metrics[0] and 'tokens: 63 ' in metrics[1]
            assert provider.recovery_calls == 1
            assert store.get_run(next_run.run_id).model_dump() == next_before
            assert store._active_run_id == next_run.run_id
            store.cancel_run(created.run_id)
            assert (await store.execute_observation_step(created.run_id, action_id=action)).model_dump() == after
        finally:
            provider.release.set()
            await task
    asyncio.run(scenario())
