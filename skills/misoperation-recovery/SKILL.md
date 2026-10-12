# Misoperation Recovery Skill (C-YD-03)

시각장애인의 물리 인덕션 터치 조작 시, 신호 미인식(missed touch)이나 신호 지연이 발생했을 때 안전하게 복구하고 최대 1회에 한해 재조작을 안내하는 핵심 안전 복구 Skill입니다.

## 1. 목적 (Purpose)
- 물리적 터치 미인식(유효한 시간창에서 변화 없음, `mismatch/none`) 발생 시 즉각적인 실패 대신 지연 관측 여부를 확인합니다.
- 지연 관측에서도 변화가 없으면, 최대 1회에 한해 새로운 조작 ID(`action_id`)를 발급하여 재조작을 안내합니다.
- 재조작 후에도 미인식이 지속되거나 센서 이상이 발견되면 추가 조작을 즉시 중단하고 `uncertain` 상태로 안전하게 종료합니다.

## 2. 입출력 계약 (Contract)

### 입력 (Inputs)
- `goal`: 사용자 원래 목표 (`Goal`: 화구, 증가, 1단계)
- `current_verdict`: 현재 판정 결과 (`Verdict`: status, level, reason_codes)
- `retry_count`: 현재 실행 내에서의 재시도 횟수 (정수, 0 이상)
- `delayed_observation`: 버퍼링된 지연 관측 데이터 (선택적)

### 출력 (Outputs / Decisions)
- `decision`:
  - `LATE_SUCCESS`: 지연 관측에서 뒤늦게 성공이 확인되어 성공 종결 (`succeeded`)
  - `RETRY_INSTRUCTION`: 첫 번째 미인식으로 재조작 안내 발행 (`awaiting_user`, 새 `action_id`, 새 `Instruction`)
  - `ABORT_UNCERTAIN`: 재시도 횟수 소진(최대 1회 초과) 또는 안전 이상으로 안전 중단 (`uncertain`)
- `next_status`: 전이될 다음 실행 상태 (`succeeded`, `awaiting_user`, `uncertain`)
- `instruction`: 새로 생성된 재조작 안내 (RETRY_INSTRUCTION인 경우)
- `message`: 사용자 음성/화면 안내 문구

## 3. 복구 규칙 및 제약 (Rules & Constraints)
1. **지연 관측 우선 확인:**
   - 관측 시간창 직후 늦게 수신된 신호가 있는지 먼저 확인하며, 늦게 성공이 감지되면 재조작을 요구하지 않고 `LATE_SUCCESS`로 종결합니다.
2. **최대 1회 재조작 제한:**
   - 맹목적인 무한 반복 안내는 화구 과열이나 화상 위험을 초래하므로 `MAX_RETRIES = 1`로 엄격히 제한합니다.
3. **새 조작 ID 발급:**
   - 재조작 안내 시 이전 `action_id`를 재사용하지 않고 새로운 UUID 기반 `action_id`를 발행하여 이전 ack와의 혼선을 방지합니다.
4. **안전 중단:**
   - 2회 연속 미인식 또는 센서 품질 불량 시 "2회 연속으로 결과를 확인하지 못했습니다. 추가 조작을 멈춰 주세요" 안내와 함께 즉시 중단합니다.

## 4. 코드 연결 (Implementation)
- 복구 엔진: `backend/app/agent/recovery.py` (`MisoperationRecoverySkill`)
- 오케스트레이터 연동: `backend/app/agent/orchestrator.py`
- 상태 머신 연동: `backend/app/api/runs.py`
