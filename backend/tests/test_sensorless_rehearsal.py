"""The rehearsal calls actual routes; injected transport failures must not multiply runs."""
import json
from contextlib import nullcontext

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.runs import run_store
from app.config import settings
from app.rehearsal.runner import RehearsalError, SCENARIOS, run_rehearsal, validate_url


@pytest.fixture
def client(monkeypatch):
    run_store.reset()
    monkeypatch.setattr(settings, 'execution_mode', 'mock_llm')
    monkeypatch.setattr(settings, 'demo_enabled', True)
    monkeypatch.setattr(settings, 'mock_scenario', 'delayed')
    with TestClient(__import__('app.main', fromlist=['app']).app, base_url='http://127.0.0.1',
                    client=('127.0.0.1', 50000)) as client:
        yield client
    run_store.reset()


@pytest.mark.parametrize('zone', ['right', 'left'])
def test_all_scenarios_repeat_through_actual_routes(client, zone):
    cases = []
    report = run_rehearsal(client, scenarios=SCENARIOS, repeat=2, zone=zone, on_case=cases.append)
    assert report['status'] == 'passed' and report['settings_restored'] is True
    assert len(cases) == 16 and cases == report['cases']
    assert len({case['run_id'] for case in cases}) == 16
    for case in cases:
        assert case['outcome'] == 'matched' and case['cleanup_confirmed']
        assert case['snapshot']['source'] == 'mock'
        assert case['snapshot']['events'] and len(case['actions']) <= 2
    assert settings.mock_scenario == 'delayed'
    assert report['physical_validation_performed'] is False


@pytest.mark.parametrize('mode', ['backend_stub', 'real_llm'])
def test_refuses_non_mock_model_before_any_mutation(client, monkeypatch, mode):
    monkeypatch.setattr(settings, 'execution_mode', mode)
    original = client.request
    calls = []
    def capture(method, path, **kwargs):
        calls.append((method, path))
        return original(method, path, **kwargs)
    monkeypatch.setattr(client, 'request', capture)
    with pytest.raises(RehearsalError):
        run_rehearsal(client)
    assert calls == [('GET', '/api/demo')]


def test_unknown_create_stops_without_retry_or_cancel_of_unknown_run(client, monkeypatch):
    original = client.request
    calls = []
    def fail_after_create(method, path, **kwargs):
        calls.append((method, path))
        response = original(method, path, **kwargs)
        if method == 'POST' and path == '/api/runs':
            raise httpx.ReadTimeout('lost response')
        return response
    monkeypatch.setattr(client, 'request', fail_after_create)
    report = run_rehearsal(client, repeat=3)
    assert report['status'] == 'blocked' and len(report['cases']) == 1
    assert report['cases'][0]['run_id'] is None
    assert report['cases'][0]['cleanup_confirmed'] is False
    assert calls.count(('POST', '/api/runs')) == 1
    assert not any('/cancel' in path for _, path in calls)
    assert report['settings_restored'] is False


def test_lost_ack_is_not_retried_and_known_run_is_safely_cancelled(client, monkeypatch):
    original = client.request
    calls = []
    lost = False
    def fail_once(method, path, **kwargs):
        nonlocal lost
        calls.append((method, path))
        response = original(method, path, **kwargs)
        if path.endswith('/ack') and not lost:
            lost = True
            raise httpx.ReadTimeout('lost ack')
        return response
    monkeypatch.setattr(client, 'request', fail_once)
    report = run_rehearsal(client)
    first = report['cases'][0]
    assert report['status'] == 'failed' and len(report['cases']) == 3
    assert first['cleanup_confirmed'] and first['snapshot']['status'] == 'stopped'
    assert sum(path.endswith('/ack') and first['run_id'] in path for _, path in calls) == 1
    assert report['cases'][1]['outcome'] == 'matched'
    assert report['settings_restored']


def test_failed_cleanup_aborts_instead_of_starting_next_run(client, monkeypatch):
    original = client.request
    calls = []
    def fail(method, path, **kwargs):
        calls.append((method, path))
        if path.endswith(('/step', '/cancel')):
            raise httpx.ReadTimeout('transport failed')
        return original(method, path, **kwargs)
    monkeypatch.setattr(client, 'request', fail)
    report = run_rehearsal(client)
    assert report['status'] == 'blocked' and len(report['cases']) == 1
    assert report['cases'][0]['cleanup_confirmed'] is False
    assert calls.count(('POST', '/api/runs')) == 1


def test_active_someone_else_run_is_not_cancelled(client):
    active = client.post('/api/runs', json={'user_input':'화력 올려줘','source':'mock','zone_id':'right'}).json()
    report = run_rehearsal(client)
    assert report['status'] == 'blocked' and report['cases'][0]['run_id'] is None
    assert client.get('/api/runs/'+active['run_id']).json()['status'] == 'awaiting_user'


@pytest.mark.parametrize('url', ['https://127.0.0.1', 'http://example.com', 'http://127.0.0.1/path',
    'http://key@127.0.0.1', 'http://127.0.0.1?api_key=x', 'http://127.0.0.1#fragment', 'http://127.0.0.1:bad'])
def test_nonlocal_or_credential_urls_rejected(url):
    with pytest.raises(ValueError): validate_url(url)


def test_cli_records_cases_exclusively_and_bounds_transport(client, monkeypatch, tmp_path):
    import app.rehearsal.__main__ as cli
    captured = []
    def factory(**kwargs):
        captured.append(kwargs)
        return nullcontext(client)
    monkeypatch.setattr(cli.httpx, 'Client', factory)
    output = tmp_path/'evidence.jsonl'
    assert cli.main(['--output',str(output),'--url','http://127.0.0.1:8002','--repeat','2']) == 0
    lines = [json.loads(line) for line in output.read_text(encoding='utf-8').splitlines()]
    assert lines[0]['kind'] == 'rehearsal_start' and lines[-1]['status'] == 'passed' and lines[-1]['kind'] == 'rehearsal_end'
    assert len([line for line in lines if line['kind'] == 'case']) == 6
    assert captured == [dict(base_url='http://127.0.0.1:8002',timeout=10.0,follow_redirects=False,trust_env=False)]
    before = output.read_bytes()
    assert cli.main(['--output',str(output)]) == 2
    assert output.read_bytes() == before


def test_invalid_options_do_not_call_server(client, monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError('must validate before network')
    monkeypatch.setattr(client, 'request', forbidden)
    for options in [{'repeat':0},{'repeat':True},{'zone':'both'},{'scenarios':['unknown']}]:
        with pytest.raises(RehearsalError): run_rehearsal(client, **options)


def test_invalid_selection_metadata_never_starts_agent(client, monkeypatch):
    original = client.request
    calls = []
    def corrupt(method, path, **kwargs):
        calls.append((method, path))
        if method == 'POST' and path == '/api/demo/scenario':
            return httpx.Response(200, json={'scenario':'normal','execution_mode':'mock_llm','source':'live'})
        return original(method, path, **kwargs)
    monkeypatch.setattr(client, 'request', corrupt)
    report = run_rehearsal(client)
    assert report['status'] == 'blocked' and ('POST','/api/runs') not in calls


def test_unexpected_terminal_is_preserved_as_failure_not_replaced_with_expected(client, monkeypatch):
    original = client.request
    def corrupt(method, path, **kwargs):
        response = original(method, path, **kwargs)
        if method == 'GET' and path.startswith('/api/runs/') and response.json()['status'] == 'succeeded':
            data = response.json()
            data['status'] = 'failed'
            return httpx.Response(200, json=data)
        return response
    monkeypatch.setattr(client, 'request', corrupt)
    report = run_rehearsal(client, scenarios=('normal',))
    case = report['cases'][0]
    assert report['status'] == 'failed' and case['outcome'] == 'unexpected'
    assert case['snapshot']['status'] == 'failed' and case['expected_run_status'] == 'succeeded'


def test_recovery_requires_new_action_even_when_server_returns_success(client, monkeypatch):
    import app.api.runs as runs
    original = runs.build_demo_adapter
    monkeypatch.setattr(runs, 'build_demo_adapter', lambda scenario, *, zone_id: original('normal', zone_id=zone_id))
    report = run_rehearsal(client, scenarios=('recovery',))
    case = report['cases'][0]
    assert case['snapshot']['status'] == 'succeeded'
    assert len(case['actions']) == 1 and case['expected_action_count'] == 2
    assert case['outcome'] == 'unexpected' and report['status'] == 'failed'
