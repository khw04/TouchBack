# 백엔드 기본 틀 (C-KH-01)

검증 환경: Windows, Python 3.12.14. Python 3.12를 사용한다.
공통 타입은 `app/contracts/models.py`에서 가져온다. 관측 v0.2, Goal/Verdict/RunEvent와 실행 API 요청·응답 형식만 구현했다.
`/health`는 서버 구동 확인이며 `agent_ready: false`를 반환한다.
실행 API, 상태 머신, Verifier, Skill, 실제 LLM과 .env 로딩은 후속 티켓이다. C-KH-02의 MockAdapter는 [Mock관측](../docs/Mock관측.md)에 사용법과 한계를 정리했다.

## 설치와 실행 (PowerShell, 저장소 루트에서)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements-dev.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

브라우저에서 http://127.0.0.1:8000/health 또는 http://127.0.0.1:8000/docs 를 연다.
현재 `/api/runs`는 404다. 모델 API 키가 없어도 서버와 테스트를 실행할 수 있다.
운영 의존성만 설치할 때는 `backend/requirements.txt`를 사용한다. 개발 파일은 테스트 의존성까지 포함한 고정 버전 목록이다.

## 검증

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m pytest -q
```

원문 JSON 예시의 로딩/왕복 변환, 잘못된 수치·버전·시간·ID, null/0 구분, 판정 수준 조합, API 자료형과 서버 구동을 검사한다.
자료형 검증은 실측 보정·관측 유효성 판정이나 실행 상태 전환을 대신하지 않는다.
LF 분리·8192바이트 한도, 순번/세션 추적, 시간 매핑과 출처 설정 검사는 어댑터 티켓에서 구현한다.
알 수 없는 필드는 거부한다. 계약 확장 시 예시·양쪽 구현을 함께 갱신한다.

리뷰 초점: 차우진은 장치 출력/숫자/단위, 윤도훈은 실행 API와 공통 타입 사용 경계를 확인한다.
