"""Reported token facts survive rejected response structures; no external calls."""
from unittest.mock import patch
import httpx
import pytest
from fastapi.testclient import TestClient
from app.api.runs import run_store
from app.config import settings
from app.main import app


@pytest.mark.parametrize("choices", [[],[None],[{"message":None}],[{"message":{},"finish_reason":"stop"}]])
def test_invalid_choices_preserve_reported_usage_without_an_instruction(choices,monkeypatch):
    monkeypatch.setattr(settings,"execution_mode","real_llm")
    monkeypatch.setattr(settings,"openai_api_key","dummy-offline-key")
    response=httpx.Response(200,request=httpx.Request("POST","https://api.openai.com/v1/chat/completions"),json={
        "model":"mock-provider-format-test","choices":choices,
        "usage":{"prompt_tokens":45,"completion_tokens":18,"total_tokens":63,"completion_tokens_details":{"reasoning_tokens":3}}})
    run_store.reset()
    try:
        with patch("httpx.AsyncClient.post",return_value=response),TestClient(app) as client:
            result=client.post('/api/runs',json={"user_input":"화력 한 단계 올려줘","source":"mock","zone_id":"right"}).json()
            run=client.get('/api/runs/'+result['run_id']).json()
            assert run['status']=='failed' and run['instruction'] is None
            metrics=[e['message'] for e in run['events'] if '토큰 메트릭' in e['message']]
            assert len(metrics)==1 and 'tokens: 63' in metrics[0] and 'reasoning: 3' in metrics[0] and 'calls: 1' in metrics[0]
            assert client.post('/api/runs',json={"user_input":"화력 한 단계 올려줘","source":"mock","zone_id":"left"}).status_code==201
    finally:run_store.reset()
