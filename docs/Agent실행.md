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

## PR #16 최신 리뷰 반영 (2026-10-17)

- 물리 조작 문장은 backend/app/agent/instructions.py의 서버 템플릿에서 생성한다. 초기 안내는 `지정 화구의 + 버튼을 한 번 눌러 주세요.`, 복구 안내는 `지정 화구의 + 버튼을 다시 한 번 눌러 주세요.`만 허용한다. 비어 있지 않은 모델 안내가 템플릿과 다르면 안전 템플릿으로 대체하며, 자유 문장은 사용자 조작 이벤트에 싣지 않는다.
- 초기 안내와 복구 안내를 게시하기 직전에 서버가 `check_sensor_integrity`와 선택 화구의 `read_field`를 필수 실행한다. 모델이 Tool 호출을 생략해도 이 검사를 생략할 수 없다. 기준 관측/자기장 요약 누락, 시계 미검증, 500ms를 벗어난 기준선, 보정 ID 누락, 포화, degraded/invalid 품질 또는 품질 사유가 있으면 안내 없이 uncertain으로 종료한다. 500ms는 기존 Mock 개발 기준이며 실측 확정값이 아니다.
- OpenAiProvider는 `refusal`, `finish_reason`, 본문 및 Tool 호출 형식을 검사한다. 거절·빈 안내·비정상 종료·Tool 루프 한도 소진(3턴)은 안전 템플릿으로 대체하지 않고 초기/복구 모두 failed로 종료한다. 활성 실행을 해제하고 현재 안내를 제거한다. 사용량이 포함된 거절/비정상 응답도 토큰 메트릭에 보존한다.
- `backend/tests/test_pr16_safety.py`는 전원 버튼, 지속 누름, 11회 누름, 부정·추가·조건부 조작, Tool 생략, 센서 품질/기준선 오류, HTTP 200의 거절·빈 응답·종료 사유를 API 및 복구 경로로 검증한다. 모의 모델/HTTP를 사용하며 외부 API 요청은 테스트에서 차단한다.

검증 결과: 신규 71개를 포함한 backend pytest 350개, 프런트 node --test 29개, git diff --check 통과. 로컬 .venv 실행기가 참조하는 Python 경로를 사용할 수 없어 설치된 Python 3.13.5와 기존 .venv 의존성으로 검증했다. 실제 OpenAI 호출·과금/사용량 대시보드·실물 센서·브라우저 TTS는 미검증이다.

응답 형식 근거: [OpenAI Chat Completions 공식 문서](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).

## 2026-10-18 추가 검토 수정: 보정 사전 검사와 지연 판정

- registry의 사전 점검과 최종 Verifier는 observation_calibration_errors를 공유한다. 보정 ID·기종/세션·화구·출처·단위/방법/샘플링·표본수와 기준 RMS 범위를 확인하며, 최초/복구 안내 전 해당 프로필과 다른 관측을 차단한다. 프로필은 여전히 Mock 전용이며 live/replay를 검증 완료로 올리지 않는다.
- 관측 수집 직전 기준선을 run/action/화구/시간창에 묶어 복사한다. 변화 없음 뒤 지연 관측이 도착하면 이 원래 기준선으로 검증한다. 재시도용으로 새로 읽은 기준선과 혼용하지 않으며 늦은 관측의 품질·시계·시간창·보정·정확 단계 검사는 유지한다. 최종 verdict 이벤트는 최종 판정의 evidence_ids를 가리킨다.
- API 회귀 30개: 좌/우 정상과 잘못된 보정 ID/단위/RMS/표본/방법/샘플링, 모델 대기 중 변경된 재시도 기준선, 변화 없음→지연 exact_step/출력 증가만/다른 단계/품질 불량/시간창 밖을 검사했다. 이 브랜치 backend 전체 388개 통과. 외부 모델/실물 검증은 아니다.
