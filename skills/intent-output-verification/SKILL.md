---
name: intent-output-verification
description: 목표와 기준·후속 관측을 결정론적으로 검사하고 확인 수준에 맞는 판정과 근거를 반환한다.
---

# 의도-출력 검증 Skill

## 목적과 현재 범위

사용자의 화력 한 단계 증가 목표와 센서 관측을 비교한다. LLM이 품질·보정 검사를 우회하거나 출력 변화만으로 정확 단계 성공을 선언하지 못하게 한다.
현재 C-KH-03은 Mock 관측과 명시적 Mock 보정만 지원한다. 실제 장비 정확도·잔열 안전·실제 LLM Agent의 완료 증거가 아니다.

## 실행 코드

진입점: `backend/app/verification/verifier.py`의 `verify_intent(goal, baseline, observations, context, calibration)`.
형식: 기존 `Goal`, `Observation`, `Verdict`와 `backend/app/verification/models.py`의 `ActionContext`, `MockCalibration`.
LLM Tool 등록과 실행 상태 전환은 윤도훈 후속 작업이다. 이 Skill 자체는 재조작/음성 안내/물리 제어를 하지 않는다.

## 입출력

- Goal: 화구 right/left, 증가, 한 단계. 관측 정답을 포함하지 않는다.
- baseline: 안내 전에 수신한 기준 관측 또는 None.
- observations: 동일 조작에서 수집한 후속 관측 배열. 지연 재조회 시 이미 받은 관측과 합친다.
- context: run/action ID, 안내부터 ack 이후 관측 종료까지의 호스트 시간 창과 현재 단조 시간. 종료 창 이전 판정은 허용하지 않는다.
- calibration: 서버가 보유한 Mock 보정 또는 None. LLM이 임의로 생성/변경하지 않는다. `source=mock`에만 적용한다.
- 출력: status, verification_level, goal_satisfied, reason_codes, evidence_ids를 가진 Verdict.

## 사용 조건과 규칙

1. 기준/후속 관측, 보정이 없거나 자료형 검증에 실패하면 uncertain/none/null.
2. 화구·장치·부팅 세션·출처·보정 ID·단위·방법·샘플링 설정을 확인한다. 품질 valid와 빈 reasons, 검증된 시간 매핑이 필요하다.
3. 기준 관측은 안내 전에 측정·수신돼야 한다. 보정의 신선함/수신 지연 조건과 테스트 기준 RMS 범위를 적용한다.
4. 후속 관측의 시간 창 전체가 조작 창에 포함돼야 한다. 시각/지연/관측 길이/전체 샘플 수와 run/action ID를 검사한다. 신선함은 측정 종료 시각으로 확인한다.
5. 동일 ID/내용은 한 번 집계한다. 같은 ID의 충돌, 순번 역행, 유효하지 않은 관측 또는 서로 다른 판정의 관측이 섞이면 성공 대신 uncertain.
6. 보정의 정확 단계 구간과 목표가 일치할 때만 success/exact_step/true. 보정된 다른 단계는 mismatch/exact_step/false.
7. 증가만 확인됐으면 uncertain/output_change/null. 유효한 관측/보정의 변화 없음은 mismatch/none/false이며 터치 미인식 확정은 아니다.
8. 비프음 횟수는 단독 성공 근거가 아니다. 자기장 관측이 누락되면 비프음이 있어도 불확실이다.

## 실행 예제 (PowerShell)

저장소 루트에서 백엔드 README에 따라 의존성을 설치한 뒤 실행한다.

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m app.verification.demo --calibration app/verification/mock-calibration.json
..\.venv\Scripts\python.exe -m app.verification.demo --calibration app/verification/mock-calibration.json --scenario no_change
..\.venv\Scripts\python.exe -m app.verification.demo --calibration app/verification/mock-calibration.json --scenario sensor_error
..\.venv\Scripts\python.exe -m pytest -q
```

첫 명령은 Mock exact_step 성공, 두 번째는 변화 없음 불일치, 세 번째는 센서 이상 불확실을 출력한다. `--scenario`는 로컬 개발 설정이며 Agent에 등록하는 입력이 아니다. Verifier 함수는 시나리오명을 받지 않는다.

## 보정의 한계와 실패 처리

`backend/app/verification/mock-calibration.json`은 테스트 신호용 명시적 설정이다. 테스트 기준 RMS 95~105, 변화 없음 허용 2, 증가 최소 5, 한 단계 증가량 18~22, 두 단계 38~42 등을 사용한다. 실측값이나 범용 인덕션 규칙으로 인용하지 않는다.
실측/replay 입력은 Mock 보정으로 통과하지 않는다. 실측 보정의 수집/검증/설치 조건/버전은 C-KH-05 및 전자팀 실측 후 확장한다.
단일 유효 시간 창으로 판별 가능한 Mock 신호만 현재 지원한다. 주기 변화·과도 신호·실물 관측 집계는 추가 실측과 정책이 필요하다.
`uncertain`에서는 자동 재조작을 권하지 않는다. 복구 Skill/Agent가 지연 관측과 원인·시도 제한을 별도로 확인해야 한다.
검증은 `backend/tests/test_verification.py`에 있으며 실제 센서/LLM 호출 없이 실행한다.
