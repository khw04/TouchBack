# TouchBack 화면 Demo 실행

## 현재 실행 가능 범위

`frontend/`는 실행 API v0.2(`docs/실행계약.md`)에 맞춘 정적 화면이다. `?fixture=1`은 **UI 화면 예시**이며 센서·Agent·서버 판정이 없다. 예시 종료 상태는 `uncertain`이고 서버 성공으로 표시하지 않는다.

```powershell
cd TouchBack
python -m http.server 8765
```

브라우저에서 `http://127.0.0.1:8765/frontend/?fixture=1`을 연다. 목표를 입력하고 `실행 시작` → `조작 완료 알림` → 실행 기록을 확인한다. 음성이 지원되지 않으면 화면에 표시되며 같은 안내는 새 event/action에서만 자동 재생된다. `안내 다시 듣기`는 사용자가 직접 누른 경우에만 재생한다.

## 백엔드 연결

권희원/윤도훈의 v0.2 서버가 `frontend/`를 같은 출처에서 제공한 뒤 `http://<서버>/frontend/`를 연다. 별도 개발 서버를 쓰면 `?api=http://127.0.0.1:8000`을 붙이고 백엔드 CORS 허용이 필요하다. 화면은 `POST /api/runs`, `GET /api/runs/{id}`, `POST /ack`, `POST /cancel`만 호출한다. `/api/session`인 1차 제출 API는 계약이 달라 자동 연결하지 않는다.

시연 순서는 목표 입력 → 서버의 계획/안내 → 사용자 실제 조작 → 조작 완료 알림 → 관측/판정/복구 이벤트 → 종료다. `ack`는 성공이 아니라 조작 완료 알림이다. 화면 상단에서 `source`(mock/replay/live)와 `execution_mode`(backend_stub/mock_llm/real_llm)를 함께 확인한다. `backend_stub`에는 준비 미완료 상태를 표시해야 한다.

## 통합 촬영 체크

1. **정상:** 새 run ID, 조작 안내, tool_call/observation/verdict, 종료 상태가 모두 보이는지 확인한다.
2. **미인식 후 복구:** 첫 verdict와 recovery, 새 action ID, 두 번째 관측/판정을 확인한다. 같은 안내의 자동 음성 중복이 없어야 한다.
3. **센서 오류:** 오류 근거와 `uncertain`/`stopped`를 확인한다. 성공 화면 또는 재조작 강요가 나오면 이슈로 등록한다.

각 실행에서 입력 출처, 실행 방식, 서버 버전/커밋, 시나리오, 장치·보정 조건, 화면 영상 파일, 이벤트 ID를 기록한다. fixture 실행과 live 실측은 별도 표로 관리한다. 실제 인덕션은 사람이 조작하고 안전 확인도 사람이 수행한다.
