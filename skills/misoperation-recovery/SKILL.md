# Misoperation Recovery Skill (C-YD-03)

시각장애인의 물리 인덕션 터치 조작 시, 신호 미인식(missed touch)이나 신호 지연이 발생했을 때 안전하게 복구하고 최대 1회에 한해 재조작을 안내하는 핵심 안전 복구 Skill입니다.

## 1. 목적 (Purpose)
- 물리적 터치 미인식(유효한 시간창에서 변화 없음, `mismatch/none/no_output_change`) 발생 시 즉각적인 실패 대신 지연 관측 여부를 확인합니다.
- 지연 도착한 관측을 정식 `verify_intent` 계약으로 재검증하여 성공이 확인되면 `succeeded`로 종결합니다.
- 지연 관측에서도 변화가 없으면 전송/시계 상태와 새 기준선(`baseline`) 확보를 확인한 뒤, 최대 1회에 한해 새로운 조작 ID(`action_id`)를 발급하여 재조작을 안내합니다.
- 관측 누락, 센서/시계 이상, 새 기준선 미확보 또는 재조작 후 미인식 지속 시 추가 조작을 즉시 중단하고 `uncertain` 상태로 안전하게 종료합니다.

## 2. 입출력 계약 (Contract)

### 입력 (Inputs)
- `goal`: 사용자 원래 목표 (`Goal`: 화구, 증가, 1단계)
- `current_verdict`: 현재 판정 결과 (`Verdict`: status, level, reason_codes, evidence_ids)
- `retry_count`: 현재 실행 내에서의 재시도 횟수 (정수, 0 이상)
- `delayed_verdict`: 지연 관측 검증 결과 (`Verdict | None`)
- `new_baseline`: 새 조작을 위한 기준 관측선 (`Observation | None`)
- `sensor_integrity_ok`: 센서 및 시계 무결성 정상 여부 (`bool`)
- `delayed_checked`: 지연 관측 확인 성공 여부 (`bool`)

### 출력 (Outputs / Decisions)
- `decision`:
  - `LATE_SUCCESS`: 지연 관측에서 뒤늦게 정식 1단 성공이 확인되어 종결 (`succeeded`)
  - `RETRY_INSTRUCTION`: 유효한 변화 없음 확인 및 새 기준선 확보 후 재조작 안내 발행 (`awaiting_user`, 새 `action_id`, 새 `Instruction`)
  - `ABORT_UNCERTAIN`: 재시도 소진(1회 초과), 관측 누락, 센서/시계 이상 또는 새 기준선 미확보로 안전 중단 (`uncertain`)
- `next_status`: 전이될 다음 실행 상태 (`succeeded`, `awaiting_user`, `uncertain`)
- `instruction`: 새로 생성된 재조작 안내 (RETRY_INSTRUCTION인 경우)
- `message`: 사용자 음성/화면 안내 문구
- `updated_verdict`: 갱신된 검증 판정 결과 (`Verdict | None`)

## 3. 복구 규칙 및 제약 (Rules & Constraints)
1. **지연 관측 우선 검증:**
   - 1차 관측 창 직후 지연 도착한 신호가 있는지 확인하고, 공통 `verify_intent`로 검증하여 정식 성공이 확인되면 재조작 없이 `LATE_SUCCESS`로 종결합니다.
2. **누락 및 불완전 신호 차단:**
   - 빈 관측(`observations_missing`)이나 센서 품질 불량(`sensor_quality_invalid`)은 유효한 변화 없음이 아니므로 재조작을 안내하지 않고 즉시 `uncertain`으로 중단합니다.
3. **전송 상태 및 새 기준선 확보 필수:**
   - 지연 관측을 확인할 수 없거나, 시계/전송 품질이 unverified/invalid이거나, 새 기준선을 확보할 수 없는 경우 안전 중단합니다.
4. **최대 1회 재조작 제한:**
   - 맹목적인 무한 반복 안내는 화구 과열이나 화상 위험을 초래하므로 `MAX_RETRIES = 1`로 엄격히 제한합니다.
5. **새 조작 ID 발급:**
   - 재조작 안내 시 이전 `action_id`를 재사용하지 않고 새로운 UUID 기반 `action_id`를 발행하여 이전 ack 및 늦은 관측 결과와의 경합을 방지합니다.

## 4. 코드 연결 (Implementation)
- 복구 엔진: `backend/app/agent/recovery.py` (`MisoperationRecoverySkill`)
- 오케스트레이터 연동: `backend/app/agent/orchestrator.py`
- 상태 머신 연동: `backend/app/api/runs.py`
