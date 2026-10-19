# Misoperation Recovery Skill (C-YD-03)

시각장애인의 물리 인덕션 터치 조작 시, 신호 미인식(missed touch)이나 신호 지연이 발생했을 때 안전하게 복구하고 최대 1회에 한해 재조작을 안내하는 핵심 안전 복구 Skill입니다.

## 1. 목적 (Purpose)
- 물리적 터치 미인식(유효한 시간창에서 변화 없음, `mismatch/none/no_output_change`) 발생 시 즉각적인 실패 대신 지연 관측 여부를 확인합니다.
- 지연 도착한 관측을 정식 `verify_intent` 계약으로 재검증하여 성공이 확인되면 `succeeded`로 종결합니다.
- 지연 관측에서 출력 증가(`output_change`), 불확실(`uncertain`), 센서 품질 이상 등이 발견되면 최초의 '변화 없음' 판정은 무효화되므로 절대 재조작을 안내하지 않고 즉시 `uncertain`으로 안전 중단합니다.
- 지연 관측까지 확인하여 확실한 '변화 없음'임이 입증되고, 센서 무결성과 신선한 새 기준선(`max_baseline_age_ms` 500ms 이내)이 확보된 경우에만 분리된 새 조작 시간창 및 새 조작 ID(`action_id`)를 발급하여 최대 1회 재조작을 안내합니다.
- 관측 누락, 센서/시계 이상, 오래된 기준선 미확보 또는 재조작 후 미인식 지속 시 추가 조작을 즉시 중단하고 `uncertain` 상태로 안전하게 종료합니다.

## 2. 입출력 계약 (Contract)

### 입력 (Inputs)
- `goal`: 사용자 원래 목표 (`Goal`: 화구, 증가, 1단계)
- `current_verdict`: 현재 판정 결과 (`Verdict`: status, level, reason_codes, evidence_ids)
- `retry_count`: 현재 실행 내에서의 재시도 횟수 (정수, 0 이상)
- `delayed_verdict`: 지연 관측 검증 결과 (`Verdict | None`)
- `new_baseline`: 새 조작을 위한 기준 관측선 (`Observation | None`)
- `now_ms`: 현재 가상 시각 (밀리초)
- `calibration`: 보정 프로필 (`MockCalibration | None`)
- `sensor_integrity_ok`: 센서 및 시계 무결성 정상 여부 (`bool`)
- `delayed_checked`: 지연 관측 확인 성공 여부 (`bool`)
- `next_window_start_ms`: 재조작 시작 시간창 (`int | None`)
- `next_window_end_ms`: 재조작 종료 시간창 (`int | None`)

### 출력 (Outputs / Decisions)
- `decision`:
  - `LATE_SUCCESS`: 지연 관측에서 뒤늦게 정식 1단 성공이 확인되어 종결 (`succeeded`)
  - `RETRY_INSTRUCTION`: 유효한 변화 없음 확인 및 신선한 새 기준선 확보 후 재조작 안내 발행 (`awaiting_user`, 새 `action_id`, 새 `Instruction`)
  - `ABORT_UNCERTAIN`: 지연 출력 감지/센서 이상, 기준선 만료(`baseline_stale`), 재시도 소진(1회 초과)으로 안전 중단 (`uncertain`)
- `next_status`: 전이될 다음 실행 상태 (`succeeded`, `awaiting_user`, `uncertain`)
- `instruction`: 새로 생성된 재조작 안내 (RETRY_INSTRUCTION인 경우)
- `message`: 사용자 음성/화면 안내 문구
- `updated_verdict`: 갱신된 검증 판정 결과 (`Verdict | None`)
- `next_window_start_ms`: 다음 조작을 위해 분리된 관측 시간창 시작 시각
- `next_window_end_ms`: 다음 조작을 위해 분리된 관측 시간창 종료 시각

## 3. 복구 규칙 및 제약 (Rules & Constraints)
1. **지연 관측 우선 검증 및 출력 변화 시 재조작 차단:**
   - 1차 관측 창 직후 지연 도착한 신호를 검증하여 성공 시 `LATE_SUCCESS`로 종결합니다.
   - 만약 지연 관측에서 출력 증가(`output_change`)나 판별 불가가 나타나면 최초 '변화 없음'은 무효화되므로 즉시 `uncertain`으로 중단합니다.
2. **신선한 새 기준선 검증 필수 (`max_baseline_age_ms`):**
   - 새 기준선이 없거나, `next_window_start_ms - new_baseline.mapped_window_end_ms > max_baseline_age_ms (500ms)`로 만료된 경우(`baseline_stale`) 즉시 안전 중단합니다.
3. **최초 조작과 재시도 조작의 시간창 분리:**
   - 재시도 조작은 1차 시간창(500~3500ms)을 재사용하지 않고, 재안내 시점 이후로 분리된 새로운 시간창(예: 4500~7500ms)을 부여합니다.
4. **최대 1회 재조작 제한:**
   - 맹목적인 무한 반복 안내는 화구 과열이나 화상 위험을 초래하므로 `MAX_RETRIES = 1`로 엄격히 제한합니다.
5. **새 조작 ID 발급:**
   - 재조작 안내 시 이전 `action_id`를 재사용하지 않고 새로운 UUID 기반 `action_id`를 발행하여 이전 ack 및 늦은 관측 결과와의 경합을 방지합니다.

## 4. 코드 연결 (Implementation)
- 복구 엔진: `backend/app/agent/recovery.py` (`MisoperationRecoverySkill`)
- 오케스트레이터 연동: `backend/app/agent/orchestrator.py`
- 상태 머신 연동: `backend/app/api/runs.py`

## 5. API 안내 게시 안전 경계 (PR #16)

복구 Skill이 RETRY_INSTRUCTION을 반환해도 API는 모델 복구 호출과 필수 센서 재점검이 통과한 경우에만 awaiting_user를 게시한다. 실제 조작 문장은 backend/app/agent/instructions.py의 지정 화구 + 버튼 1회 누름 템플릿을 사용하며 지속 누름·추가 조작을 허용하지 않는다. 모델 거절·빈 응답·처리 불가능한 모델 오류는 failed, 센서/기준선 점검 실패는 uncertain으로 종료하며 현재 안내를 제거한다. 모델 호출 중 악화된 센서 상태도 게시 직전 재점검한다. 관련 회귀 검사는 backend/tests/test_pr16_safety.py에 있다.


## 2026-10-19 Claude 관측 Agent 적용

real_llm에서는 모델이 관측 요약 조회, 필요 시 지연 관측/재시도 안전성 검토, 다음 행동(완료·중단·1회 재안내·사용자 확인 교정 제안)을 Tool로 선택한다. 복구 Skill의 허용 판단만으로 재안내를 게시하지 않는다. LLM 선택과 기존 품질/시간/보정/재시도 조건을 모두 통과해야 한다. 물리 조작 문장은 서버 템플릿이며 자동 제어/자동 교정은 없다. mock_llm은 기존 연습 흐름으로 남는다. 설정은 TOUCHBACK_LLM_PROVIDER=anthropic, ANTHROPIC_API_KEY, CLAUDE_MODEL이며 상세 계약·실제 호출 표본·한계는 [Agent판단](../../docs/Agent판단.md)을 따른다.
