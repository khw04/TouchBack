"""Native Claude Messages adapter; shared provider contract, no secret-bearing errors."""
import json
import httpx
from app.config import Settings
from app.llm.base import BaseLlmProvider, LlmResponse, LlmUsage, ToolCall
from app.llm.openai_provider import (MissingApiKeyError, LlmAuthenticationError,
    LlmRateLimitError, LlmTimeoutError, LlmProviderError, LlmResponseFormatError)


def claude_messages(messages):
    system=[]
    converted=[]
    for message in messages:
        role=message['role']
        if role=='system':
            system.append(message.get('content') or '')
            continue
        blocks=[]
        if role=='tool':
            role='user'
            blocks.append({'type':'tool_result','tool_use_id':message['tool_call_id'],
                           'content':message.get('content') or ''})
        else:
            if message.get('content'):
                blocks.append({'type':'text','text':message['content']})
            for call in message.get('tool_calls') or []:
                blocks.append({'type':'tool_use','id':call['id'],'name':call['function']['name'],
                               'input':json.loads(call['function']['arguments'])})
        if not blocks:
            continue
        if converted and converted[-1]['role']==role:
            converted[-1]['content'].extend(blocks)
        else:
            converted.append({'role':role,'content':blocks})
    return '\n'.join(system),converted


class ClaudeProvider(BaseLlmProvider):
    def __init__(self, settings: Settings):
        self.settings=settings
        self.api_url='https://api.anthropic.com/v1/messages'

    async def generate(self,messages,tools=None):
        key=self.settings.anthropic_api_key.strip()
        if not key:
            raise MissingApiKeyError('ANTHROPIC_API_KEY가 설정되지 않았습니다.')
        system,converted=claude_messages(messages)
        payload={'model':self.settings.claude_model,'max_tokens':2048,
                 'system':system,'messages':converted,'thinking':{'type':'disabled'}}
        if tools:
            payload['tools']=[{'name':t['function']['name'],'description':t['function'].get('description',''),
                               'input_schema':t['function']['parameters']} for t in tools]
            payload['tool_choice']={'type':'auto'}
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response=await client.post(self.api_url,json=payload,headers={
                    'x-api-key':key,'anthropic-version':'2023-06-01','content-type':'application/json'})
                response.raise_for_status()
                data=response.json()
        except httpx.TimeoutException as exc:
            raise LlmTimeoutError('Claude API 요청 시간이 초과되었습니다.') from exc
        except httpx.HTTPStatusError as exc:
            code=exc.response.status_code
            if code in (401,403):raise LlmAuthenticationError('Claude API 인증/접근 권한을 확인하세요.') from exc
            if code==429:raise LlmRateLimitError('Claude API 한도 또는 요청 제한입니다.') from exc
            raise LlmProviderError(f'Claude API HTTP {code}. 서버 모델 설정과 제공자 상태를 확인하세요.') from exc
        except httpx.RequestError as exc:
            raise LlmProviderError('Claude API 네트워크 요청에 실패했습니다.') from exc
        except ValueError as exc:
            raise LlmResponseFormatError('Claude 응답이 JSON이 아닙니다.') from exc
        return self.parse(data,tools)

    def parse(self,data,tools):
        usage=None
        model=self.settings.claude_model
        if isinstance(data,dict):
            model=data.get('model') if isinstance(data.get('model'),str) else model
            raw=data.get('usage')
            if isinstance(raw,dict) and 'input_tokens' in raw and 'output_tokens' in raw:
                counts=[raw.get(k,0) for k in ('input_tokens','output_tokens','cache_read_input_tokens','cache_creation_input_tokens')]
                if all(type(n) is int and n>=0 for n in counts):
                    inp,out,read,write=counts
                    usage=LlmUsage(prompt_tokens=inp+read+write,completion_tokens=out,total_tokens=inp+read+write+out)
        def invalid(reason):
            return LlmResponseFormatError(reason,usage=usage,model=model)
        if not isinstance(data,dict) or not isinstance(data.get('content'),list):
            raise invalid('Claude 응답 content 형식 오류입니다.')
        if usage is None:
            raise invalid('Claude 사용량 정보가 없거나 올바르지 않습니다.')
        calls=[];text=[];ids=set()
        for block in data['content']:
            if not isinstance(block,dict):raise invalid('Claude 응답 블록 형식 오류입니다.')
            if block.get('type')=='text' and isinstance(block.get('text'),str):
                text.append(block['text'])
            elif block.get('type')=='tool_use':
                cid,name,args=block.get('id'),block.get('name'),block.get('input')
                if not isinstance(cid,str) or not cid or cid in ids or not isinstance(name,str) or not name or not isinstance(args,dict):
                    raise invalid('Claude Tool 이름/ID/인수 형식 오류입니다.')
                ids.add(cid);calls.append(ToolCall(id=cid,name=name,arguments=args))
            else:
                raise invalid('지원하지 않는 Claude 응답 블록입니다.')
        stop=data.get('stop_reason')
        if calls:
            if stop!='tool_use' or not tools:raise invalid('허용되지 않은 Claude Tool 종료입니다.')
        elif stop!='end_turn' or not ''.join(text).strip():
            raise invalid('Claude 응답 거절/미완료/빈 결과입니다.')
        return LlmResponse(text='\n'.join(text) or None,tool_calls=calls,usage=usage or LlmUsage(),model=model)
