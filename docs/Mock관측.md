# Mock 관측 어댑터 (C-KH-02)

이 구현은 실물 없이 관측 Tool을 개발하기 위한 입력 계층이다. 센서 정확도·정확한 화력 단계·Agent 성공을 입증하지 않는다.
관측 v0.2와 공통 타입을 유지하며 실행 API와 LLM Tool 등록은 포함하지 않는다.

## 로컬에서 사용

가상 시계로 대기하므로 실제 3초를 기다리지 않는다. Python을 backend 폴더에서 실행한다.

```python
from app.adapters.mock_setup import build_mock_adapter

adapter = build_mock_adapter("delayed")  # 개발 설정. 사용자/Agent 입력으로 받지 않는다.
baseline = adapter.read_baseline("right")
observations = adapter.await_touch_result(
    run_id="run-1", action_id="action-1", zone_id="right",
    window_start_ms=500, window_end_ms=3500, timeout_ms=3000,
)
# 첫 조회는 빈 배열. 같은 조작의 늦은 관측을 확인한다.
late = adapter.await_touch_result(
    run_id="run-1", action_id="action-1", zone_id="right",
    window_start_ms=500, window_end_ms=3500, timeout_ms=1000,
)
quality = adapter.check_sensor_integrity()
```

## 제공 경계

- `read_baseline(zone_id)`: 해당 화구에서 도착한 최신 관측 또는 None. 기준으로 사용할 수 있는지는 후속 Verifier가 품질·시각·보정으로 검사한다.
- `await_touch_result(...)`: 가상 시계를 timeout_ms만큼 진행하고 지정한 측정 창과 겹치는 도착 관측을 반환한다. 반환 관측에 run/action을 연결한다. 창 경계를 걸친 관측도 버리지 않으므로 Verifier가 전체 포함 여부를 검사해야 한다.
- `check_sensor_integrity()`: 최신 수신 관측의 품질/시간 매핑 또는 전송 오류 상태. 최신 값이 있더라도 신선함을 보장하지 않는다. 누락은 빈 관측 배열과 후속 시간 검증으로 처리한다.
- 관측은 한 어댑터에서 후속 조회에 한 번만 반환한다. 호출자는 이미 받은 근거를 보관하고 늦은 조회의 새 관측을 합쳐야 한다. 기준 조회는 후속 조회를 소비하지 않는다.

## 테스트 상황

| 로컬 설정 | 입력 동작 |
|---|---|
| normal | 기준 100 → 후속 RMS 120, 비프음 1 |
| no_change | RMS 100 유지, 비프음 0 |
| missing | 후속 관측 없음 |
| delayed | 측정 창 1000~1500ms, 수신 4500ms |
| duplicate | 동일 관측을 재전송하되 한 번만 반환 |
| sensor_error | 자기장·비프음 null, 품질 invalid/disconnected |

모든 수치는 테스트 값이다. 설정 이름·평가 정답·예상 판정은 반환 관측에 들어가지 않는다.
`mock-test-v1`은 테스트용 보정 식별자만 제공한다. C-KH-03에서 이 ID와 대응하는 명시적 Mock 보정 파일/Verifier를 추가했다. ID만으로 성공을 선언하지 않으며 보정·관측·시간 검사를 통과해야 한다. 실제 단계 보정 근거는 아니다.

## 시간과 입력 무결성

장치·호스트는 같은 제어된 가상 단조 시계와 고정 UTC 기준을 사용한다. 모의 일정에서 5개 연속 고유 관측을 받은 뒤에만 clock_quality=verified로 표시한다. 순번 공백이면 확인 횟수를 다시 센다.
이 매핑은 Mock 환경에 한정한다. live의 지연 추정/보정 검증을 대신하지 않는다.
측정 창과 수신 시각을 분리하고, 늦은 관측의 측정 창을 현재 시각으로 바꾸지 않는다.
중복 ID는 제외하며 순번 역행/세션 변경은 명시적 오류로 노출한다. 한 어댑터는 한 장치·세션이며 세션 복구는 C-KH-04에서 구현한다.
화구가 다른 관측은 반환하지 않고, 반환값/입력 일정은 복사해 외부 수정이 내부 근거를 바꾸지 않게 한다.

## 다음 연결 작업

C-KH-03은 이 입력에 유효성/시간/보정 검사와 의도 검증 Skill을 붙인다.
윤도훈은 실행 상태·ack·취소·동시 실행 제한을 제공하고 이 어댑터의 관측 조회를 Tool에 연결한다.
실제 LLM, 실시간 대기, Serial/Replay 입력과 물리 장비 검증은 별도 작업이다.
