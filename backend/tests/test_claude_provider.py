"""Native Claude HTTP contract tests. All requests are intercepted."""
import asyncio
from unittest.mock import AsyncMock
import httpx
import pytest
from app.config import Settings
from app.llm.claude_provider import ClaudeProvider,claude_messages
from app.llm.openai_provider import (LlmAuthenticationError,LlmRateLimitError,LlmProviderError,
    LlmTimeoutError,LlmResponseFormatError,MissingApiKeyError)


def response(content=None,stop='end_turn',**changes):
    return {'model':'offline-claude','content':content or [{'type':'text','text':'확인'}],
        'stop_reason':stop,'usage':{'input_tokens':10,'output_tokens':3,'cache_read_input_tokens':4,'cache_creation_input_tokens':2},**changes}


def provider():return ClaudeProvider(Settings(anthropic_api_key='fake-local-test-key'))


def test_native_round_trip_tool_messages_and_usage(monkeypatch):
    messages=[{'role':'system','content':'system'}, {'role':'user','content':'request'},
        {'role':'assistant','content':None,'tool_calls':[{'id':'t1','function':{'name':'check','arguments':'{}'}}]},
        {'role':'tool','tool_call_id':'t1','content':'{"success":true}'}]
    post=AsyncMock(return_value=httpx.Response(200,json=response(),request=httpx.Request('POST','https://api.anthropic.com/v1/messages')))
    monkeypatch.setattr(httpx.AsyncClient,'post',post)
    result=asyncio.run(provider().generate(messages,[{'type':'function','function':{'name':'check','parameters':{'type':'object','properties':{}}}}]))
    payload=post.call_args.kwargs['json']
    assert post.call_args.args[0]=='https://api.anthropic.com/v1/messages'
    assert payload['system']=='system' and payload['messages'][1]['content'][0]['type']=='tool_use'
    assert payload['messages'][2]['content'][0]['tool_use_id']=='t1'
    assert payload['tools'][0]['input_schema']['type']=='object'
    assert post.call_args.kwargs['headers']['anthropic-version']=='2023-06-01'
    assert result.usage.prompt_tokens==16 and result.usage.total_tokens==19
    assert result.model=='offline-claude'


@pytest.mark.parametrize('stop',['max_tokens','refusal','pause_turn','stop_sequence',None])
def test_incomplete_responses_preserve_usage(stop):
    with pytest.raises(LlmResponseFormatError) as err:provider().parse(response(stop=stop),None)
    assert err.value.usage.total_tokens==19


@pytest.mark.parametrize('block',[
    {'type':'tool_use','id':'t','name':'x','input':[]},
    {'type':'tool_use','id':'','name':'x','input':{}},
    {'type':'refusal','text':'no'}, {'type':'thinking','thinking':'secret'},42])
def test_bad_tool_or_block_fails_closed(block):
    with pytest.raises(LlmResponseFormatError):provider().parse(response([block],'tool_use'),[{}])


def test_tools_require_matching_stop_and_unique_ids():
    block={'type':'tool_use','id':'t','name':'x','input':{}}
    result=provider().parse(response([block],'tool_use'),[{}])
    assert result.tool_calls[0].name=='x'
    for blocks,stop,tools in [([block],'end_turn',[{}]),([block],'tool_use',None),([block,block],'tool_use',[{}])]:
        with pytest.raises(LlmResponseFormatError):provider().parse(response(blocks,stop),tools)


@pytest.mark.parametrize('code,error',[(401,LlmAuthenticationError),(403,LlmAuthenticationError),(429,LlmRateLimitError),(400,LlmProviderError),(529,LlmProviderError)])
def test_http_failures_never_echo_secrets(monkeypatch,code,error):
    post=AsyncMock(return_value=httpx.Response(code,text='fake-local-test-key sensitive body',request=httpx.Request('POST','https://api.anthropic.com/v1/messages')))
    monkeypatch.setattr(httpx.AsyncClient,'post',post)
    with pytest.raises(error) as err:asyncio.run(provider().generate([{'role':'user','content':'x'}]))
    assert 'sensitive' not in str(err.value) and 'fake-local-test-key' not in str(err.value)


def test_missing_key_and_timeout_do_not_fallback(monkeypatch):
    with pytest.raises(MissingApiKeyError):asyncio.run(ClaudeProvider(Settings(anthropic_api_key='')).generate([]))
    monkeypatch.setattr(httpx.AsyncClient,'post',AsyncMock(side_effect=httpx.ReadTimeout('secret')))
    with pytest.raises(LlmTimeoutError):asyncio.run(provider().generate([{'role':'user','content':'x'}]))


def test_config_provider_precedence_and_key_redaction(monkeypatch):
    monkeypatch.setenv('TOUCHBACK_LLM_PROVIDER','anthropic');monkeypatch.setenv('CLAUDE_MODEL','offline-config-model')
    monkeypatch.setenv('ANTHROPIC_API_KEY','fake-local-test-key')
    settings=Settings.load()
    assert settings.model_name=='offline-config-model' and settings.is_llm_configured()
    assert 'fake-local-test-key' not in repr(settings)
    settings.llm_provider='openai';settings.openai_api_key=''
    assert not settings.is_llm_configured()
    with pytest.raises(ValueError):Settings(llm_provider='untrusted')


@pytest.mark.parametrize('usage',[None,{},[],{'input_tokens':-1,'output_tokens':2},{'input_tokens':True,'output_tokens':2}])
def test_invalid_usage_is_not_invented_as_zero(usage):
    with pytest.raises(LlmResponseFormatError) as err:provider().parse(response(usage=usage),None)
    assert err.value.usage is None
