"""Agent Orchestrator (C-YD-02): plan, instruct, observe tools, and verify intent."""
from typing import Any
import uuid

from app.agent.prompts import get_verdict_message
from app.agent.recovery import MisoperationRecoverySkill, RecoveryResult, default_recovery_skill
from app.agent.verifier import IntentVerifier, default_verifier
from app.contracts.models import (
    Goal,
    Instruction,
    RunEvent,
    RunStatus,
    Source,
    Verdict,
    Zone,
)
from app.llm.base import BaseLlmProvider
from app.llm.mock_provider import MockLlmProvider
from app.tools.base import ToolRegistry
from app.tools.registry import default_tool_registry


class AgentOrchestrator:
    """사용자 목표 수신부터 센서 관측, 의도 검증 및 오조작 복구까지 총괄하는 Agent 오케스트레이터."""

    def __init__(
        self,
        llm_provider: BaseLlmProvider | None = None,
        tool_registry: ToolRegistry | None = None,
        verifier: IntentVerifier | None = None,
        recovery_skill: MisoperationRecoverySkill | None = None,
    ) -> None:
        self.llm = llm_provider or MockLlmProvider()
        self.tools = tool_registry or default_tool_registry
        self.verifier = verifier or default_verifier
        self.recovery = recovery_skill or default_recovery_skill

    def interpret_goal(
        self,
        user_input: str,
        zone_id: Zone | None,
    ) -> tuple[Goal | None, str | None]:
        """사용자 발화를 분석하여 P0 유효 목표(Goal)를 도출합니다."""
        text = user_input.lower().strip()
        unsupported = ["꺼", "종료", "꺼줘", "내려", "감소", "낮춰", "줄여", "정지", "차단", "off", "중단"]
        if any(kw in text for kw in unsupported):
            return None, f"지원하지 않거나 준비되지 않은 목표입니다: '{user_input}'. P0에서는 화력 1단 증가만 지원합니다."

        supported = ["올려", "증가", "높여", "키워", "1단", "한 단계", "켜", "시작", "+"]
        if not any(kw in text for kw in supported):
            return None, f"지원하지 않거나 불명확한 목표입니다: '{user_input}'. P0에서는 화력 1단 증가('화력 1단 올려줘')만 지원합니다."

        if zone_id is None:
            return None, "화구가 지정되지 않았습니다. 조작할 화구(왼쪽/오른쪽)를 선택해 주세요."

        return Goal(zone_id=zone_id, direction="increase", requested_steps=1), None

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
        window_ms: int = 3000,
    ) -> tuple[Verdict, dict[str, Any], str]:
        """관측 도구를 실행하고 Verifier를 통해 판정을 도출하며 사용자 피드백 문구를 생성합니다."""
        # 1. 센서 관측 도구 호출
        obs_res = await self.tools.execute(
            "await_touch_result",
            action_id=action_id,
            window_ms=window_ms,
        )

        evidence_id = f"ev-{action_id}"

        # 관측 도구 실패 또는 센서 어댑터 미설정
        if not obs_res.success:
            verdict = Verdict(
                status="uncertain",
                verification_level="none",
                goal_satisfied=None,
                reason_codes=[obs_res.error or "sensor_observation_failed"],
                evidence_ids=[evidence_id],
            )
            feedback = get_verdict_message("uncertain", "none")
            return verdict, obs_res.data, feedback

        # 2. 의도 검증 수행
        verdict = self.verifier.verify(
            goal=goal,
            observation_data=obs_res.data,
            evidence_id=evidence_id,
        )

        # 3. 판정에 따른 사용자 안내 피드백 문구 생성
        feedback = get_verdict_message(verdict.status, verdict.verification_level)
        return verdict, obs_res.data, feedback

    def determine_next_status(self, verdict: Verdict) -> RunStatus:
        """docs/실행계약.md 상태 전이 규칙에 따라 다음 Run 상태를 결정합니다."""
        if verdict.status == "success" and verdict.verification_level == "exact_step":
            return "succeeded"
        if verdict.status == "mismatch" and verdict.verification_level == "none":
            # 유효한 변화 없음 -> 복구 가능 상태 (recovering)
            return "recovering"
        # 단순 증가 또는 품질/센서 이상 -> uncertain
        return "uncertain"

    def handle_recovery(
        self,
        goal: Goal,
        verdict: Verdict,
        retry_count: int,
        delayed_observation: dict[str, Any] | None = None,
    ) -> RecoveryResult:
        """오조작 복구 Skill을 호출하여 재조작 안내, 지연 성공 또는 안전 중단을 결정합니다."""
        return self.recovery.evaluate(
            goal=goal,
            current_verdict=verdict,
            retry_count=retry_count,
            delayed_observation=delayed_observation,
        )
