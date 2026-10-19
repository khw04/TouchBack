"""Misoperation recovery skill implementation (C-YD-03, skills/misoperation-recovery/SKILL.md)."""
from enum import Enum
import uuid

from app.agent.instructions import instruction_text
from app.contracts.models import Goal, Instruction, Observation, RunStatus, Verdict
from app.verification.models import MockCalibration


class RecoveryDecisionType(str, Enum):
    LATE_SUCCESS = "late_success"
    RETRY_INSTRUCTION = "retry_instruction"
    ABORT_UNCERTAIN = "abort_uncertain"


class RecoveryResult:
    def __init__(
        self,
        decision: RecoveryDecisionType,
        next_status: RunStatus,
        instruction: Instruction | None = None,
        message: str = "",
        updated_verdict: Verdict | None = None,
        next_window_start_ms: int = 500,
        next_window_end_ms: int = 3500,
    ) -> None:
        self.decision = decision
        self.next_status = next_status
        self.instruction = instruction
        self.message = message
        self.updated_verdict = updated_verdict
        self.next_window_start_ms = next_window_start_ms
        self.next_window_end_ms = next_window_end_ms


class MisoperationRecoverySkill:
    """오조작 복구 Skill (C-YD-03, skills/misoperation-recovery/SKILL.md)."""

    MAX_RETRIES: int = 1

    def evaluate(
        self,
        goal: Goal,
        current_verdict: Verdict,
        retry_count: int,
        delayed_verdict: Verdict | None = None,
        new_baseline: Observation | None = None,
        now_ms: int | None = None,
        calibration: MockCalibration | None = None,
        sensor_integrity_ok: bool = True,
        delayed_checked: bool = True,
        next_window_start_ms: int | None = None,
        next_window_end_ms: int | None = None,
    ) -> RecoveryResult:
        """복구 전략을 엄격히 평가하여 지연 성공, 1회 재조작 안내, 또는 안전 중단을 결정합니다.

        규칙:
        1. 지연 도착한 관측에서 정식 1단 증가 성공이 검증된 경우 -> LATE_SUCCESS (succeeded)
        2. 지연 관측이 존재하는데 성공이 아니고 output_change/uncertain/센서이상 등인 경우 -> 즉시 안전 중단 (ABORT_UNCERTAIN)
        3. 최초 관측이 '유효한 변화 없음(no_output_change)'이 아닌 경우 -> 즉시 안전 중단 (ABORT_UNCERTAIN)
        4. 지연 관측을 확인할 수 없거나(delayed_checked=False), 센서 무결성 이상(sensor_integrity_ok=False) -> 즉시 안전 중단 (ABORT_UNCERTAIN)
        5. 새 기준선 미확보 또는 품질 불량, 또는 신선도 검사 실패(stale baseline) -> 즉시 안전 중단 (ABORT_UNCERTAIN)
        6. 최대 재시도(1회)를 이미 소진한 경우 -> 즉시 안전 중단 (ABORT_UNCERTAIN)
        7. 위 모든 조건을 통과한 경우에만 신선한 새 기준선 및 분리된 새 시간창으로 1회 재조작 안내 발행 (RETRY_INSTRUCTION)
        """
        # 1. 지연 관측 판정 검증
        if delayed_verdict is not None:
            # 1-A: 지연 성공(exact_step success)
            if (
                delayed_verdict.status == "success"
                and delayed_verdict.verification_level == "exact_step"
                and delayed_verdict.goal_satisfied is True
            ):
                return RecoveryResult(
                    decision=RecoveryDecisionType.LATE_SUCCESS,
                    next_status="succeeded",
                    instruction=None,
                    message=("지연된 신호에서 화력 감소가 확인되었습니다. 화력이 한 단계 내려갔습니다."
                             if goal.direction == "decrease" else
                             "지연된 신호에서 화력 증가가 확인되었습니다. 화력이 한 단계 올라갔습니다."),
                    updated_verdict=delayed_verdict,
                )

            # 1-B: 지연 관측이 존재하지만 유효한 '변화 없음'이 아닌 경우 (output_change, uncertain, sensor_quality_invalid 등)
            is_delayed_valid_no_change = (
                delayed_verdict.status == "mismatch"
                and delayed_verdict.verification_level == "none"
                and "no_output_change" in delayed_verdict.reason_codes
            )
            if not is_delayed_valid_no_change:
                # 지연 관측에서 출력이 감지되었거나 판정 불가/센서 이상이 확인된 경우
                # 최초의 '변화 없음' 판정은 재시도 근거가 될 수 없으므로 절대 재조작을 안내하지 않고 즉시 안전 중단
                abort_verdict = Verdict(
                    status="uncertain",
                    verification_level="none",
                    goal_satisfied=None,
                    reason_codes=delayed_verdict.reason_codes or ["delayed_observation_uncertain"],
                    evidence_ids=current_verdict.evidence_ids + delayed_verdict.evidence_ids,
                )
                return RecoveryResult(
                    decision=RecoveryDecisionType.ABORT_UNCERTAIN,
                    next_status="uncertain",
                    instruction=None,
                    message="지연 관측에서 출력 변화 또는 판정 불가가 확인되었습니다. 안전을 위해 추가 조작을 멈춰 주세요.",
                    updated_verdict=abort_verdict,
                )

        # 2. 최초 관측이 '유효한 변화 없음(no_output_change)'인지 엄격히 검사
        is_valid_no_change = (
            current_verdict.status == "mismatch"
            and current_verdict.verification_level == "none"
            and "no_output_change" in current_verdict.reason_codes
        )

        if not is_valid_no_change:
            abort_verdict = Verdict(
                status="uncertain",
                verification_level="none",
                goal_satisfied=None,
                reason_codes=current_verdict.reason_codes or ["unrecoverable_observation"],
                evidence_ids=current_verdict.evidence_ids,
            )
            return RecoveryResult(
                decision=RecoveryDecisionType.ABORT_UNCERTAIN,
                next_status="uncertain",
                instruction=None,
                message="조작 결과를 신뢰할 수 없습니다. 안전을 위해 추가 조작을 멈춰 주세요.",
                updated_verdict=abort_verdict,
            )

        # 3. 지연 관측 확인 가능 여부 및 센서 무결성 검증
        if not delayed_checked:
            abort_verdict = Verdict(
                status="uncertain",
                verification_level="none",
                goal_satisfied=None,
                reason_codes=["delayed_observation_unverified"],
                evidence_ids=current_verdict.evidence_ids,
            )
            return RecoveryResult(
                decision=RecoveryDecisionType.ABORT_UNCERTAIN,
                next_status="uncertain",
                instruction=None,
                message="지연 관측을 확인할 수 없습니다. 안전을 위해 추가 조작을 멈춰 주세요.",
                updated_verdict=abort_verdict,
            )

        if not sensor_integrity_ok:
            abort_verdict = Verdict(
                status="uncertain",
                verification_level="none",
                goal_satisfied=None,
                reason_codes=["sensor_integrity_compromised"],
                evidence_ids=current_verdict.evidence_ids,
            )
            return RecoveryResult(
                decision=RecoveryDecisionType.ABORT_UNCERTAIN,
                next_status="uncertain",
                instruction=None,
                message="센서 신호 상태가 불안정합니다. 안전을 위해 추가 조작을 멈추고 인덕션을 확인해 주세요.",
                updated_verdict=abort_verdict,
            )

        # 4. 새 기준선 확보 및 신선도(freshness) / 유효성 검증
        if (
            new_baseline is None
            or new_baseline.device_observation.quality.status != "valid"
            or new_baseline.clock_quality != "verified"
        ):
            abort_verdict = Verdict(
                status="uncertain",
                verification_level="none",
                goal_satisfied=None,
                reason_codes=["baseline_unavailable_or_invalid"],
                evidence_ids=current_verdict.evidence_ids,
            )
            return RecoveryResult(
                decision=RecoveryDecisionType.ABORT_UNCERTAIN,
                next_status="uncertain",
                instruction=None,
                message="새 조작을 위한 기준 신호를 확보하지 못했습니다. 안전을 위해 추가 조작을 멈춰 주세요.",
                updated_verdict=abort_verdict,
            )

        # 기준선 신선도 검사 (새 조작 시작 시점 대비 기준선 만료 검사)
        target_start_ms = next_window_start_ms if next_window_start_ms is not None else now_ms
        max_age = calibration.max_baseline_age_ms if calibration else 500
        if target_start_ms is not None:
            age = target_start_ms - new_baseline.mapped_window_end_ms
            if age < 0 or age > max_age:
                abort_verdict = Verdict(
                    status="uncertain",
                    verification_level="none",
                    goal_satisfied=None,
                    reason_codes=["baseline_stale"],
                    evidence_ids=current_verdict.evidence_ids + [new_baseline.observation_id],
                )
                return RecoveryResult(
                    decision=RecoveryDecisionType.ABORT_UNCERTAIN,
                    next_status="uncertain",
                    instruction=None,
                    message="새 조작을 위한 기준 신호가 너무 오래되었습니다. 안전을 위해 추가 조작을 멈춰 주세요.",
                    updated_verdict=abort_verdict,
                )

        if calibration is not None and new_baseline.device_observation.field_summary is not None:
            rms = new_baseline.device_observation.field_summary.rms
            if not (calibration.baseline_rms_min <= rms <= calibration.baseline_rms_max):
                abort_verdict = Verdict(
                    status="uncertain",
                    verification_level="none",
                    goal_satisfied=None,
                    reason_codes=["baseline_outside_calibration"],
                    evidence_ids=current_verdict.evidence_ids + [new_baseline.observation_id],
                )
                return RecoveryResult(
                    decision=RecoveryDecisionType.ABORT_UNCERTAIN,
                    next_status="uncertain",
                    instruction=None,
                    message="새 조작을 위한 기준 신호가 보정 범위를 벗어났습니다. 안전을 위해 추가 조작을 멈춰 주세요.",
                    updated_verdict=abort_verdict,
                )

        # 5. 최대 재시도(1회) 소진 여부 확인
        if retry_count >= self.MAX_RETRIES:
            abort_verdict = Verdict(
                status="uncertain",
                verification_level="none",
                goal_satisfied=None,
                reason_codes=["max_retry_exhausted_no_change"],
                evidence_ids=current_verdict.evidence_ids,
            )
            return RecoveryResult(
                decision=RecoveryDecisionType.ABORT_UNCERTAIN,
                next_status="uncertain",
                instruction=None,
                message="재조작 후에도 화력 변화를 확인하지 못했습니다. 안전을 위해 추가 조작을 멈추고 인덕션 상태를 직접 확인해 주세요.",
                updated_verdict=abort_verdict,
            )

        # 6. 모든 조건 충족 시: 분리된 새 시간창 및 새 action_id로 1회 재조작 안내 발행
        new_action_id = f"act-retry-{uuid.uuid4().hex[:6]}"
        instruction = Instruction(
            action_id=new_action_id,
            text=instruction_text(goal, is_recovery=True),
        )
        final_start = target_start_ms if target_start_ms is not None else 500
        final_end = next_window_end_ms if next_window_end_ms is not None else (final_start + 3000)
        return RecoveryResult(
            decision=RecoveryDecisionType.RETRY_INSTRUCTION,
            next_status="awaiting_user",
            instruction=instruction,
            message=instruction.text,
            updated_verdict=current_verdict,
            next_window_start_ms=final_start,
            next_window_end_ms=final_end,
        )


default_recovery_skill = MisoperationRecoverySkill()
