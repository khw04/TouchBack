"""Intent output verification bridge based on docs/판정기준.md and v0.2 contracts."""
from typing import Any
from app.contracts.models import Goal, Verdict


class IntentVerifier:
    """사용자의 목표와 센서 관측 결과를 대조하여 Verdict를 판정하는 브리지."""

    def verify(
        self,
        goal: Goal,
        observation_data: dict[str, Any],
        evidence_id: str = "obs-mock-1",
    ) -> Verdict:
        """관측 데이터를 기반으로 docs/판정기준.md에 정의된 5대 판정 경로를 도출합니다."""
        # 1. 센서 오류, 품질 불량, 또는 미연결 상태 점검
        obs_status = observation_data.get("status")
        quality = observation_data.get("quality", "valid")
        if obs_status in ("invalid", "degraded", "error") or quality in ("invalid", "degraded"):
            return Verdict(
                status="uncertain",
                verification_level="none",
                goal_satisfied=None,
                reason_codes=["sensor_quality_degraded_or_uncalibrated"],
                evidence_ids=[evidence_id],
            )

        # 관측 수치 추출
        field_delta = float(observation_data.get("field_delta_rms", 0.0))
        beep_count = int(observation_data.get("beep_count", 0))
        step_detected = observation_data.get("step_detected")

        # 2. 유효한 시간창에서 변화 없음 (delta < 5.0 및 beep == 0)
        # 비프음 단독으로는 성공할 수 없으나, 변화 없음 확인 시에는 비프음도 없어야 함
        if field_delta < 5.0 and beep_count == 0:
            return Verdict(
                status="mismatch",
                verification_level="none",
                goal_satisfied=False,
                reason_codes=["no_output_change_detected"],
                evidence_ids=[evidence_id],
            )

        # 3. 명시적 단계 감지 기반 판정 (step_detected 제공 시)
        if step_detected is not None:
            if step_detected == goal.requested_steps:
                return Verdict(
                    status="success",
                    verification_level="exact_step",
                    goal_satisfied=True,
                    reason_codes=["exact_step_verified"],
                    evidence_ids=[evidence_id],
                )
            elif step_detected > goal.requested_steps:
                return Verdict(
                    status="mismatch",
                    verification_level="exact_step",
                    goal_satisfied=False,
                    reason_codes=["step_mismatch_over_increment"],
                    evidence_ids=[evidence_id],
                )

        # 4. 수치 임계값 기반 판별 (Mock 기본값: delta >= 30.0은 1단 증가 성공)
        if field_delta >= 30.0 and beep_count >= 1:
            return Verdict(
                status="success",
                verification_level="exact_step",
                goal_satisfied=True,
                reason_codes=["exact_step_verified_by_delta_and_beep"],
                evidence_ids=[evidence_id],
            )

        # 5. 단순 출력 증가만 확인되고 정확한 단계 구분 불가
        if field_delta >= 10.0:
            return Verdict(
                status="uncertain",
                verification_level="output_change",
                goal_satisfied=None,
                reason_codes=["output_increased_step_unverified"],
                evidence_ids=[evidence_id],
            )

        # 6. 기타/판별 불가
        return Verdict(
            status="uncertain",
            verification_level="none",
            goal_satisfied=None,
            reason_codes=["unclassified_observation"],
            evidence_ids=[evidence_id],
        )


default_verifier = IntentVerifier()
