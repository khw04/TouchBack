# TouchBack 백엔드

검증 환경: Windows, Python 3.12.14. Python 3.12를 사용한다.
공통 계약·Mock/Serial/Replay 어댑터·의도 검증·Mock 보정/상태, 실행 API·설정/Tool/provider, Agent 관측/검증·복구 코드가 PR #5~12로 병합됐다. PR #13은 Windows `.mjs` 응답 형식과 모듈 캐시를 수정했다.

`/health`는 서버 구동 확인이며 `agent_ready: false`를 반환한다. 기본 생성 API는 `backend_stub`다. 현재 작업 브랜치에는 실제 LLM 호출/Tool 실행 루프·어댑터 주입·사용량 연결 코드와 명시적 Mock 시연이 있다. 실제 외부 호출과 사용량의 운영 검증은 C-YD-04의 남은 작업이다. 상세 진행은 [작업현황](../docs/작업현황.md)을 따른다.

## 설치와 실행 (PowerShell, 저장소 루트에서)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements-dev.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

브라우저에서 http://127.0.0.1:8000/health 또는 http://127.0.0.1:8000/docs 를 연다.
기본 화면은 `http://127.0.0.1:8000/`, 화면 연습은 `http://127.0.0.1:8000/?fixture=1`이다. 정적 파일은 `/frontend/`가 아닌 `/`에서 제공한다.
현재 `/api/runs`는 backend_stub 모드로 POST/GET/ack/step/cancel을 제공한다. live 출처는 미준비 오류로 거부하고 미지원·부정·다단계 목표는 조작 안내 없이 명확화한다. `/ack`는 observing으로 전환하고 화면이 같은 action_id의 `/step`을 요청한다. `/step`은 중복 처리를 막고 관측·검증·복구를 10초 안에 처리한다. 기본 Agent에는 센서 어댑터가 없어 `uncertain`으로 종료한다. 종료 상태의 취소는 기존 결과를 보존한다. 실제 LLM·관측의 운영 연결은 후속 작업이다. 모델 API 키가 없어도 서버와 테스트를 실행할 수 있다.
운영 의존성만 설치할 때는 `backend/requirements.txt`를 사용한다. 개발 파일은 테스트 의존성까지 포함한 고정 버전 목록이다.

## 검증

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m pytest -q
```

원문 JSON 예시의 로딩/왕복 변환, 잘못된 수치·버전·시간·ID, null/0 구분, 판정 수준 조합, API 자료형과 서버 구동을 검사한다.
자료형 검증은 실측 보정·관측 유효성 판정이나 실행 상태 전환을 대신하지 않는다.
LF 분리·8192바이트 한도, 순번/세션 추적, 시간 매핑과 출처 설정 검사는 어댑터 코드와 테스트에서 구현했다. 실제 ESP32의 전송 지연·시계 매핑·분리/재부팅 검증은 남아 있다.
알 수 없는 필드는 거부한다. 계약 확장 시 예시·양쪽 구현을 함께 갱신한다.

리뷰 초점: 차우진은 장치 출력/숫자/단위, 윤도훈은 실행 API와 공통 타입 사용 경계를 확인한다.


현재 가상 시연 확장: 연속 좌우 상태, 양쪽 관측 검증·진단, 단일 증감 목표, 사용자 확인 뒤 별도 교정 실행, Mock 보정/재동기화/재보정 Tool을 제공한다. POST /api/demo/device/reset은 활성 실행 중 거부하고 별도 요청일 때만 5/5로 초기화한다. 가상 코일 관측은 채널당 200Hz의 처리된 신호로 가정하며 고주파 원시 파형이나 실측 근거가 아니다. 운영자 내부 상태와 시나리오는 모델/Tool 입력에 넣지 않는다. 상세 범위는 [모의관측범위](../docs/모의관측범위.md)를 따른다.


## 2026-10-19 Claude 관측 Agent 적용

real_llm에서는 모델이 관측 요약 조회, 필요 시 지연 관측/재시도 안전성 검토, 다음 행동(완료·중단·1회 재안내·사용자 확인 교정 제안)을 Tool로 선택한다. 복구 Skill의 허용 판단만으로 재안내를 게시하지 않는다. LLM 선택과 기존 품질/시간/보정/재시도 조건을 모두 통과해야 한다. 물리 조작 문장은 서버 템플릿이며 자동 제어/자동 교정은 없다. mock_llm은 기존 연습 흐름으로 남는다. 설정은 TOUCHBACK_LLM_PROVIDER=anthropic, ANTHROPIC_API_KEY, CLAUDE_MODEL이며 상세 계약·실제 호출 표본·한계는 [Agent판단](../docs/Agent판단.md)을 따른다.
