"""Misoperation recovery skill implementation (C-YD-03, skills/misoperation-recovery/SKILL.md)."""
from enum import Enum
import uuid

from app.contracts.models import Goal, Instruction, Observation, RunStatus, Verdict


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
    ) -> None:
        self.decision = decision
        self.next_status = next_status
        self.instruction = instruction
        self.message = message
        self.updated_verdict = updated_verdict


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
        sensor_integrity_ok: bool = True,
        delayed_checked: bool = True,
    ) -> RecoveryResult:
        """복구 전략을 엄격히 평가하여 지연 성공, 1회 재조작 안내, 또는 안전 중단을 결정합니다.

        규칙:
        1. 지연 도착한 관측에서 정식 1단 증가 성공이 검증된 경우 -> LATE_SUCCESS (succeeded)
        2. 관측 누락, 불완전, 센서 오류, 단계 불일치 등 유효한 '변화 없음'이 아닌 경우 -> 즉시 안전 중단 (ABORT_UNCERTAIN)
        3. 지연 관측을 확인할 수 없거나(delayed_checked=False), 센서 무결성 이상(sensor_integrity_ok=False),
           새 기준선 미확보(new_baseline=None)인 경우 -> 즉시 안전 중단 (ABORT_UNCERTAIN)
        4. 최대 재시도(1회)를 이미 소진한 경우 -> 즉시 안전 중단 (ABORT_UNCERTAIN)
        5. 유효한 변화 없음 확인 + 지연 검사 완료 + 센서 정상 + 새 기준선 확보 + retry_count == 0인 경우에만 -> RETRY_INSTRUCTION (awaiting_user)
        """
        # 1. 지연 관측에서 뒤늦게 정식 성공(exact_step)이 확인되었는지 확인
        if (
            delayed_verdict is not None
            and delayed_verdict.status == "success"
            and delayed_verdict.verification_level == "exact_step"
            and delayed_verdict.goal_satisfied is True
        ):
            return RecoveryResult(
                decision=RecoveryDecisionType.LATE_SUCCESS,
                next_status="succeeded",
                instruction=None,
                message="지연된 신호에서 화력 증가가 확인되었습니다. 화력이 한 단계 올라갔습니다.",
                updated_verdict=delayed_verdict,
            )

        # 2. 최초 관측이 '유효한 변화 없음(no_output_change)'인지 엄격히 검사
        is_valid_no_change = (
            current_verdict.status == "mismatch"
            and current_verdict.verification_level == "none"
            and "no_output_change" in current_verdict.reason_codes
        )

        if not is_valid_no_change:
            # 누락/불완전/불유효/다단계 불일치 등은 재조작을 금지하고 즉시 안전 중단
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

        # 3. 지연 관측 확인 가능 여부, 센서 무결성 및 새 기준선 확보 상태 검증
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

        # 4. 최대 재시도(1회) 소진 여부 확인
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

        # 5. 모든 조건 충족 시: 1회 재조작 안내 발행
        new_action_id = f"act-retry-{uuid.uuid4().hex[:6]}"
        zone_label = "오른쪽" if goal.zone_id == "right" else "왼쪽"
        instruction = Instruction(
            action_id=new_action_id,
            text=f"화력 변화가 확인되지 않았습니다. {zone_label} 화구의 + 버튼을 다시 한 번 꾹 눌러 주세요.",
        )
        return RecoveryResult(
            decision=RecoveryDecisionType.RETRY_INSTRUCTION,
            next_status="awaiting_user",
            instruction=instruction,
            message=instruction.text,
            updated_verdict=current_verdict,
        )


default_recovery_skill = MisoperationRecoverySkill()
