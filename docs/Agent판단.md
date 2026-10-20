# Claude 관측 Agent와 안전 경계

2026-10-19 사용자 요청으로 실제 제공자의 기본값을 Claude로 전환했다. 이 문서는 최신 작업 브랜치의 코드/검증 범위이며 main 병합이나 전체 제품 완료를 뜻하지 않는다.

## 역할 분리

- 서버: 목표 허용 문법, 기준/후속 관측의 출처·품질·시간·보정 검증, 단일 실행 잠금, 정확 단계 성공 허용, 재시도 1회 상한, 물리 조작 템플릿을 강제한다.
- LLM 계획: 초기 사전 점검 Tool을 선택하고 결과를 받아 안내를 수립한다. 필수 사전 점검은 LLM이 생략해도 서버가 수행한다.
- LLM 관측 판단: 검증 결과를 조회하고, 필요한 추가 안전성 검토를 선택한 뒤 완료/중단/제한된 재안내/교정 제안을 선택한다. 기존의 재시도 결정 뒤 문장 생성만 하는 real_llm 경로를 대체한다.
- 사용자: 실제 또는 가상 버튼 조작, 교정 목표 확인, 새 실행 시작을 직접 한다. 자동 물리 제어는 없다.

## 관측 단계 Tool 루프

| Tool | 결과와 권한 |
|---|---|
| inspect_observation | 현재 Goal·검증 Verdict·좌우 진단·근거 ID·시도 횟수. 반드시 먼저 조회해야 함 |
| assess_retry_safety | 유효한 변화 없음에서만 원래 조작의 지연 관측·새 기준·센서/보정/횟수 조건을 기존 복구 Skill로 검증. 반복 호출해도 한 번만 수집 |
| choose_next_action | complete / stop / retry / offer_correction 중 선택. 근거 ID와 서버 허용 조건 검증 후에만 반영 |

complete는 검증된 exact_step 성공일 때만 허용한다. retry는 지연 관측과 새 기준이 검증된 경우만 허용하고 게시 직전 필수 센서 점검을 다시 한다. offer_correction은 검증된 단일 변화의 기존 교정 후보가 있을 때만 제공하며 새 실행을 자동 생성하지 않는다. stop은 항상 선택할 수 있고 교정 버튼도 제공하지 않는다. 같은 관측에서도 모델은 중단을 선택할 수 있다.

한 응답에 Tool 하나, 최대 4회 모델 호출. 자유 문장으로 성공/조작을 선언할 수 없다. 금지된 선택에는 오류 결과를 돌려줘 모델이 다시 결정하게 하고, 한도 소진/모델 오류는 failed로 끝낸다. 10초 관측 처리 상한·취소·세대 검사·step 중복 방지는 기존 계약을 유지한다. 늦은 모델 결과는 실행을 변경하지 않고 보고된 사용량만 기록한다.

LLM에는 내부 화력, 누른 버튼, 시나리오명, 원시 로그/정답 라벨을 제공하지 않는다. 관측 요약과 결정론적 검증 결과로 판단한다. 모델의 자유 추론 과정은 수집하지 않는다. events의 관측 Agent Tool 및 Agent 다음 행동 선택과 근거 ID로 실행을 확인한다. 기존 LLM 복구 토큰 메트릭 이름은 호환성을 위해 유지하며 관측 판단 호출까지 포함한다.

## 제공자 설정

.env.example을 참고해 로컬 .env에 설정한다. 기존 .env를 덮어쓰지 않는다.

```dotenv
TOUCHBACK_LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=
CLAUDE_MODEL=claude-haiku-5-5
```

다른 접근 가능한 Claude 모델은 CLAUDE_MODEL로 명시한다. 자동 상위 모델 전환은 없다. OpenAI 경로는 TOUCHBACK_LLM_PROVIDER=openai와 기존 OPENAI_API_KEY/OPENAI_MODEL을 명시하면 유지된다. 키 누락/인증/한도/네트워크 실패 시 다른 업체나 Mock으로 대체하지 않는다. 제공자·모델·키 설정 여부만 로컬 화면에 표시하고 키는 반환하지 않는다.

화면은 `/`에서 시연 설정 → 실제 AI → 적용. 센서 입력은 여전히 Mock이다. backend_stub는 준비/안전 계약용, mock_llm은 기존 결정론적 화면 연습용이며 실제 모델의 관측 판단 루프를 실행했다는 증거가 아니다. 실제 판단을 시연하려면 real_llm과 Claude 호출/Tool 이벤트를 확인한다.

## 실제 호출에서 확인한 범위

별도 TestClient 프로세스에서 실제 Claude Haiku 5.5와 Mock 관측으로 실행했다. 정상 1회는 inspect→complete, 다른 화구 변화 1회는 inspect→stop, 미인식 1회는 inspect→assess_retry_safety→retry 뒤 두 번째 조작 inspect→complete, 센서 오류 1회는 inspect→stop이었다. 모두 종료 후 활성 잠금이 해제됐다. 전체 실행 시간은 각각 약 6.61/5.39/8.74/4.83초였다. 이 네 실행은 모델 19회 호출, 입력 28,038/출력 2,638토큰이었다. 작은 개발 확인 표본이며 정확도/성공률/고정 지연 보장이 아니다.

실물 센서/인덕션, 실제 음성 인식/접근성 사용자 평가, 다양한 표현·모든 오류 조건의 통계적 모델 평가는 미검증이다. 초기 목표 해석은 여전히 결정론적 지원 문법이며 임의 자연어·복합 조작을 지원한다고 주장하지 않는다. 온도/진동/레시피 등 기존 미구현 범위도 바뀌지 않는다.

연결 근거: [Claude Tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview), [모델 목록](https://platform.claude.com/docs/en/models/overview). 모델 선택 기본값은 개발 설정이며 최종 성능 인증이 아니다.


검증: 전체 백엔드 540개와 프런트 85개 통과. 백엔드는 backend_stub/demo=false/빈 제공자 키 환경에서 실행했고 기존 deprecation warning 6개가 있다. Claude HTTP 주입/판단 루프 신규 회귀 41개에 모델 선택, 근거 없는 성공/재시도 차단, 중단/교정 선택, 중복 step/안전성 검토, 취소·시간 초과와 늦은 사용량 보존을 포함한다. Node 구문·Python compileall·git diff --check도 통과했다. LSP/Ruff/Pylint/Pyright는 설치되어 있지 않아 실행하지 못했다.

실제 FastAPI+브라우저에서는 Claude 키 설정 여부/모델 표시, 오른쪽 요청 후 왼쪽 변화에서 실제 모델의 stop 선택, 미인식에서 assess_retry_safety→retry 선택과 두 번째 조작 후 complete를 확인했다. 1280×720 한 화면 표시, 브라우저 경고/오류 없음도 확인했다. 실제 센서/실제 음성 인식/사용자 접근성 평가/통계적 모델 성능은 미검증이다. 키와 원본 PDF는 변경·출력·커밋하지 않았다.


## PR #35 리뷰: 실패 후 Tool 사실 보존

관측 Tool이 끝나면 worker가 원래 run/action에 tool_call 사실을 한 번 기록한다. 후속 모델 오류/자유 문장/한도 소진/취소/시간 초과와 독립적이며, 늦은 사실 기록이 실행 상태·판정·다음 실행 잠금을 변경하지 않는다. 상태 반영에 성공한 행동 선택 plan과 실제 Tool 실행 사실을 구분한다. 5개 회귀에서 수정 전 누락을 재현하고 수정 후 기록 보존·중복 step 멱등·종료 상태 보호를 확인한다.
