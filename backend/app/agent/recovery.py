"""Misoperation recovery skill implementation (C-YD-03, skills/misoperation-recovery/SKILL.md)."""
from enum import Enum
from typing import Any
import uuid

from app.contracts.models import Goal, Instruction, RunStatus, Verdict


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
        delayed_observation: dict[str, Any] | None = None,
    ) -> RecoveryResult:
        """복구 전략을 평가하여 지연 성공, 1회 재조작 안내, 또는 안전 중단을 결정합니다."""
        # 1. 지연 관측에서 뒤늦게 성공이 포착되었는지 우선 확인
        if delayed_observation is not None:
            delta = float(delayed_observation.get("field_delta_rms", 0.0))
            beep = int(delayed_observation.get("beep_count", 0))
            obs_status = delayed_observation.get("status", "valid")
            if delta >= 30.0 and beep >= 1 and obs_status == "valid":
                late_verdict = Verdict(
                    status="success",
                    verification_level="exact_step",
                    goal_satisfied=True,
                    reason_codes=["late_observation_success_verified"],
                    evidence_ids=current_verdict.evidence_ids + ["ev-delayed-1"],
                )
                return RecoveryResult(
                    decision=RecoveryDecisionType.LATE_SUCCESS,
                    next_status="succeeded",
                    instruction=None,
                    message="지연된 신호에서 화력 증가가 확인되었습니다. 화력이 한 단계 올라갔습니다.",
                    updated_verdict=late_verdict,
                )

        # 2. 변화 없음(mismatch/none)에 대한 재조작 안내 여부 판정
        if current_verdict.status == "mismatch" and current_verdict.verification_level == "none":
            if retry_count < self.MAX_RETRIES:
                # 1회차 재조작 안내 발행
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
            else:
                # 최대 재시도 초과 -> 안전 중단
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

        # 3. 그 외 센서 불량이나 다단계 오조작 시에는 재시도 없이 안전 중단
        abort_verdict = Verdict(
            status="uncertain",
            verification_level="none",
            goal_satisfied=None,
            reason_codes=["unrecoverable_mismatch_or_sensor_issue"],
            evidence_ids=current_verdict.evidence_ids,
        )
        return RecoveryResult(
            decision=RecoveryDecisionType.ABORT_UNCERTAIN,
            next_status="uncertain",
            instruction=None,
            message="조작 결과를 신뢰할 수 없습니다. 안전을 위해 추가 조작을 멈춰 주세요.",
            updated_verdict=abort_verdict,
        )


default_recovery_skill = MisoperationRecoverySkill()
