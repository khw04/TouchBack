"""Agent Orchestrator (C-YD-02): plan, instruct, observe, and verify intent."""
from typing import Any
import uuid

from app.contracts.goal_policy import interpret_p0_goal
from app.agent.prompts import get_verdict_message
from app.agent.recovery import MisoperationRecoverySkill, RecoveryResult, default_recovery_skill
from app.agent.verifier import IntentVerifier, default_verifier
from app.contracts.models import (
    Goal,
    Instruction,
    Observation,
    RunStatus,
    Verdict,
    Zone,
)
from app.llm.base import BaseLlmProvider
from app.llm.mock_provider import MockLlmProvider
from app.tools.base import ToolRegistry
from app.verification.models import ActionContext


class AgentOrchestrator:
    """사용자 목표 수신부터 센서 관측, 의도 검증 및 오조작 복구까지 총괄하는 Agent 오케스트레이터."""

    def __init__(
        self,
        llm_provider: BaseLlmProvider | None = None,
        adapter: Any | None = None,
        verifier: IntentVerifier | None = None,
        recovery_skill: MisoperationRecoverySkill | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self.llm = llm_provider or MockLlmProvider()
        self.adapter = adapter
        self.verifier = verifier or default_verifier
        self.recovery = recovery_skill or default_recovery_skill
        self.tools = tool_registry

    def interpret_goal(
        self,
        user_input: str,
        zone_id: Zone | None,
    ) -> tuple[Goal | None, str | None]:
        """사용자 발화를 분석하여 P0 유효 목표(Goal)를 도출합니다."""
        return interpret_p0_goal(user_input, zone_id)

    def build_instruction(self, goal: Goal, action_id: str | None = None) -> Instruction:
        """목표에 맞는 물리 버튼 조작 안내를 생성합니다."""
        act_id = action_id or f"act-{uuid.uuid4().hex[:6]}"
        zone_label = "오른쪽" if goal.zone_id == "right" else "왼쪽"
        return Instruction(
            action_id=act_id,
            text=f"{zone_label} 화구의 + 버튼을 한 번 눌러 주세요.",
        )

    async def execute_observation_and_verification(
        self,
        goal: Goal,
        action_id: str,
        run_id: str = "run-default",
        window_start_ms: int = 500,
        window_end_ms: int = 3500,
        timeout_ms: int = 3000,
    ) -> tuple[Verdict, str]:
        """관측 어댑터에서 신호를 수집하고 verify_intent 계약으로 엄격히 검증합니다."""
        if self.adapter is None:
            # 미설정 센서인 경우 안전하게 uncertain 종료
            verdict = Verdict(
                status="uncertain",
                verification_level="none",
                goal_satisfied=None,
                reason_codes=["sensor_adapter_unconfigured"],
                evidence_ids=[],
            )
            feedback = get_verdict_message("uncertain", "none")
            return verdict, feedback

        # 1. 기준선(baseline) 조회
        baseline: Observation | None = None
        if hasattr(self.adapter, "read_baseline"):
            baseline = self.adapter.read_baseline(goal.zone_id)

        # 2. 터치 관측 결과 수집
        observations: list[Observation] = []
        if hasattr(self.adapter, "await_touch_result"):
            observations = self.adapter.await_touch_result(
                run_id=run_id,
                action_id=action_id,
                zone_id=goal.zone_id,
                window_start_ms=window_start_ms,
                window_end_ms=window_end_ms,
                timeout_ms=timeout_ms,
            )
            # 1차 시간창 내 미도착 시 지연 도착 신호 추가 확인
            if not observations and hasattr(self.adapter, "clock"):
                late_obs = self.adapter.await_touch_result(
                    run_id=run_id,
                    action_id=action_id,
                    zone_id=goal.zone_id,
                    window_start_ms=window_start_ms,
                    window_end_ms=window_end_ms,
                    timeout_ms=1500,
                )
                if late_obs:
                    observations = late_obs

        # 3. ActionContext 구성
        now_ms = getattr(getattr(self.adapter, "clock", None), "now_ms", window_end_ms)
        if now_ms < window_end_ms:
            now_ms = window_end_ms

        context = ActionContext(
            run_id=run_id,
            action_id=action_id,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
            now_ms=now_ms,
        )

        # 4. 의도 검증 실행
        verdict = self.verifier.verify(
            goal=goal,
            baseline=baseline,
            observations=observations,
            context=context,
        )

        feedback = get_verdict_message(verdict.status, verdict.verification_level)
        return verdict, feedback

    def determine_next_status(self, verdict: Verdict) -> RunStatus:
        """docs/실행계약.md 상태 전이 규칙에 따라 다음 Run 상태를 결정합니다."""
        if verdict.status == "success" and verdict.verification_level == "exact_step":
            return "succeeded"
        if (
            verdict.status == "mismatch"
            and verdict.verification_level == "none"
            and "no_output_change" in verdict.reason_codes
        ):
            # 오직 '유효한 변화 없음'일 때만 복구 루프 진입
            return "recovering"
        # 관측 누락(observations_missing), 품질 불량, 단계 불일치 등은 안전 중단
        return "uncertain"

    def handle_recovery(
        self,
        goal: Goal,
        verdict: Verdict,
        retry_count: int,
        run_id: str = "run-default",
        action_id: str = "act-default",
        window_start_ms: int = 500,
        window_end_ms: int = 3500,
        delayed_timeout_ms: int = 1000,
    ) -> RecoveryResult:
        """오조작 복구 Skill을 호출하여 지연 성공, 재조작 안내, 또는 안전 중단을 결정합니다."""
        if self.adapter is None:
            return self.recovery.evaluate(
                goal=goal,
                current_verdict=verdict,
                retry_count=retry_count,
                delayed_checked=False,
            )

        # 1. 원래 action에 대한 지연 도착 관측 확인 (delayed frame 수집)
        delayed_verdict: Verdict | None = None
        delayed_obs: list[Observation] = []
        if hasattr(self.adapter, "await_touch_result"):
            delayed_obs = self.adapter.await_touch_result(
                run_id=run_id,
                action_id=action_id,
                zone_id=goal.zone_id,
                window_start_ms=window_start_ms,
                window_end_ms=window_end_ms,
                timeout_ms=delayed_timeout_ms,
            )

        baseline = self.adapter.read_baseline(goal.zone_id) if hasattr(self.adapter, "read_baseline") else None
        now_ms = getattr(getattr(self.adapter, "clock", None), "now_ms", window_end_ms)
        if now_ms < window_end_ms:
            now_ms = window_end_ms

        if delayed_obs:
            delayed_context = ActionContext(
                run_id=run_id,
                action_id=action_id,
                window_start_ms=window_start_ms,
                window_end_ms=window_end_ms,
                now_ms=now_ms,
            )
            delayed_verdict = self.verifier.verify(
                goal=goal,
                baseline=baseline,
                observations=delayed_obs,
                context=delayed_context,
            )

        # 2. 센서 무결성 점검
        sensor_integrity_ok = True
        if hasattr(self.adapter, "check_sensor_integrity"):
            quality = self.adapter.check_sensor_integrity()
            sensor_integrity_ok = quality.status == "valid" and not quality.reasons

        # 3. 새 기준선(new baseline) 확보 및 시간창 분리
        new_baseline = self.adapter.read_baseline(goal.zone_id) if hasattr(self.adapter, "read_baseline") else None
        calibration = self.verifier.get_calibration_for_zone(goal.zone_id)
        retry_window_start_ms = now_ms
        retry_window_end_ms = now_ms + 3000

        return self.recovery.evaluate(
            goal=goal,
            current_verdict=verdict,
            retry_count=retry_count,
            delayed_verdict=delayed_verdict,
            new_baseline=new_baseline,
            now_ms=now_ms,
            calibration=calibration,
            sensor_integrity_ok=sensor_integrity_ok,
            delayed_checked=True,
            next_window_start_ms=retry_window_start_ms,
            next_window_end_ms=retry_window_end_ms,
        )
