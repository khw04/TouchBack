# Agent 실행 설정과 검증 계약

2026-10-14. 설정 로더·Tool/provider 기반(PR #11), Agent 관측·검증·복구 코드(PR #12), 목표 안전 검증(PR #15)에 이어 C-YD-04에서 실제 LLM Multi-turn Tool Calling 루프, 실행 모드(`mock_llm`, `real_llm`) 및 어댑터 자동 주입, 토큰 사용량 메트릭 기록이 구현 완료됐다.

## 입력과 실행 모드

- `backend_stub`: 기본 개발/계약 회귀 모드. 센서 미설정 시 `sensor_adapter_unconfigured` uncertain 안전 종료를 검증한다.
- `mock_llm`: 비용/키 없이 동작하는 모의 LLM + Mock 센서 모드. Multi-turn Tool Calling 루프, 사전 상태 점검, 오조작 복구 재안내, 토큰 사용량 집계 및 정상/복구/오류 3대 시나리오 E2E를 검증한다.
- `real_llm`: 실제 OpenAI API(gpt-6-luna 기본값)를 호출하는 운영 모드. `OPENAI_API_KEY`가 필수이며, 키 누락 시 모의 모델로 속이지 않고 `status="failed"` 및 `provider_waiting` 명시적 에러 이벤트를 남기고 안전 중단한다.

Mock 센서는 비프음 횟수·자기장 변화·측정 품질 등의 테스트 관측을 반환한다. 시나리오 정답/명칭은 Agent 입력에서 숨기고 평가 측에 둔다. 같은 관측 계약을 사용해 실제 입력으로 교체한다.

## 환경변수

루트 .env.example을 설정 템플릿으로 사용한다. 실제 키는 각 개발 환경의 로컬 .env 또는 서버 환경변수에만 둔다. 환경변수가 파일보다 우선하고 백엔드가 작업 디렉터리에 의존하지 않고 저장소 루트 .env를 읽는다(`backend/app/config.py`).

| 설정 | 값/용도 |
|---|---|
| OPENAI_API_KEY | 실제 키. 예제는 빈 값이며 미설정 시 real_llm 모드에서 provider_waiting으로 안전 실패 처리 |
| OPENAI_MODEL | gpt-6-luna 초기 기본값. 교체 가능한 설정 |
| OPENAI_REASONING_EFFORT | low 초기 기본값. payload reasoning_effort 반영 |
| TOUCHBACK_EXECUTION_MODE | backend_stub / mock_llm / real_llm 실행 모드 지정 |

현재 `backend/app/llm/openai_provider.py`는 Chat Completions 경로를 구현하며, `reasoning_effort` 및 세분화된 HTTP 예외(`LlmAuthenticationError`, `LlmRateLimitError`, `LlmTimeoutError`, `LlmProviderError`)를 처리한다.

`TOUCHBACK_EXECUTION_MODE` 환경변수를 통해 `mock_llm` 또는 `real_llm`으로 전환하면 실행 API(`runs.py`)가 해당 모드로 실행을 생성하고, 관측 단계에서 `MockAdapter`를 자동으로 주입하여 성공/복구/종료 경로를 완주한다. LLM은 목표 분석, 사전 점검 도구(`check_sensor_integrity`, `read_field`) 호출, 조작 안내문 도출 및 복구 재안내 생성을 맡고 관측 유효성·안전 게이트·최대 추가 조작 횟수(1회)·성공 허용 조건은 결정론적 Python 코드가 엄격히 강제한다.
Luna는 최종 성능 검증 모델이 아니다. 복잡한 오류 상황에서 부족하면 설정/프롬프트를 확인하고 상위 모델을 동일 시나리오로 비교한다. 지원 한도가 있다는 이유로 자동 결제나 모델 교체를 하지 않는다.

## 오류와 비밀키

키 누락·인증 실패·시간 초과·사용 한도·잘못된 Tool 인수·모델 응답 오류를 명시적 오류로 처리한다. 센서 값을 조작하거나 모의 모델로 몰래 대체해 성공을 반환하지 않는다. 재시도와 취소는 실행계약에 따른다.
키를 브라우저·ESP32·요청/응답 로그·실행 이벤트·PR·영상에 포함하지 않는다. .env는 .gitignore로 제외하고 .env.example만 커밋한다.
개발 도구에서 사용하는 API와 제품 Agent의 호출 기록을 구분하되 같은 지원 계정이면 비용은 합산한다. 팀원별 키와 용도별 구분이 가능한지는 발급 방식 확인 후 정한다.

## 사용량과 완료 조건

지원 한도는 팀 전체 20만 원이며 개발과 Agent 실행 모두 가능하다는 사용자 확인을 기준으로 한다. 실제 지원 공지·키 발급·결제/대시보드 조건은 확인 전 미완료다.
실행별 모델·호출 수·입력/완료/추론 토큰을 `events`(`kind="plan"`, `kind="tool_call"`)에 투명하게 기록한다. 추론 토큰을 출력에 다시 더해 중복 집계하지 않는다. 개발 도구 사용량은 제공자 대시보드와 함께 확인한다.
가격과 환율을 고정된 최신 사실로 넣지 않는다. 측정일의 적용 요금과 기준으로 추정 비용을 기록하고 실제 청구와 구분한다. 최초 1일 사용량 확인 후 남은 기간의 개발/테스트/시연 예산을 배분한다.
Mock 센서로 정상(Success), 변화 없음 후 제한된 복구(Recovery), 센서 이상/누락에서 완료 보류(Uncertain)의 3대 시나리오를 검증했다. 목표·계획·Tool·근거·재계획·최종 안내가 실행 기록에 남고 고정 성공 분기로 대체되지 않는다.

참고: [모델 기능/설정](https://developers.openai.com/api/docs/models/gpt-6-luna), [함수 호출](https://developers.openai.com/api/docs/guides/function-calling). 구현 시 현재 SDK 호환 버전을 고정한다.
