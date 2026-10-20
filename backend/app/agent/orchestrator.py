"""Agent Orchestrator (C-YD-02): plan, instruct, observe, and verify intent."""
import asyncio
from dataclasses import dataclass
import json
from typing import Any
import uuid

from app.contracts.goal_policy import interpret_p0_goal
from app.agent.instructions import instruction_text
from app.agent.prompts import SYSTEM_PROMPT, get_verdict_message
from app.agent.recovery import MisoperationRecoverySkill, RecoveryDecisionType, RecoveryResult, default_recovery_skill
from app.agent.verifier import IntentVerifier, default_verifier
from app.contracts.models import (
    Goal,
    Instruction,
    Observation,
    RunStatus,
    Verdict,
    Zone,
)
from app.llm.base import BaseLlmProvider, LlmUsage
from app.llm.mock_provider import MockLlmProvider
from app.llm.openai_provider import (
    LlmAuthenticationError,
    LlmProviderError,
    LlmRateLimitError,
    LlmResponseFormatError,
    LlmTimeoutError,
    MissingApiKeyError,
)
from app.tools.base import ToolRegistry, ToolResult
from app.tools.registry import default_tool_registry
from app.verification.models import ActionContext

FATAL_LLM_ERRORS = (
    MissingApiKeyError,
    LlmAuthenticationError,
    LlmRateLimitError,
    LlmTimeoutError,
    LlmProviderError,
    LlmResponseFormatError,
    TimeoutError,
    asyncio.TimeoutError,
    json.JSONDecodeError,
    KeyError,
    IndexError,
)


def validate_instruction_against_goal(
    text: str,
    goal: Goal,
    is_recovery: bool = False,
) -> tuple[bool, str]:
    """Only an entire server-owned template is a valid physical instruction."""
    if text.strip() != instruction_text(goal, is_recovery=is_recovery):
        return False, "검증된 단일 조작 템플릿과 불일치"
    return True, "유효한 조작 안내"


@dataclass
class UsageSummary:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    call_count: int = 0
    model: str = ""

    def record(self, usage: LlmUsage | None, model: str = "") -> None:
        if usage:
            self.prompt_tokens += usage.prompt_tokens
            self.completion_tokens += usage.completion_tokens
            self.reasoning_tokens += usage.reasoning_tokens
            self.total_tokens += usage.total_tokens
        self.call_count += 1
        if model:
            self.model = model

    def to_event_message(self) -> str:
        parts = [f"model: {self.model or 'unknown'}"]
        parts.append(f"calls: {self.call_count}")
        parts.append(
            f"tokens: {self.total_tokens} (prompt: {self.prompt_tokens}, completion: {self.completion_tokens}"
        )
        if self.reasoning_tokens > 0:
            parts[-1] += f", reasoning: {self.reasoning_tokens}"
        parts[-1] += ")"
        return ", ".join(parts)


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
        self.tools = tool_registry or default_tool_registry
        self.usage = UsageSummary()
        self.executed_tool_calls: list[dict[str, Any]] = []
        self._action_baselines: dict[tuple, Observation | None] = {}
        self._action_profiles = {}

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
        return Instruction(
            action_id=act_id,
            text=instruction_text(goal),
        )

    async def plan_and_instruct(
        self,
        user_input: str,
        zone_id: Zone | None,
        action_id: str | None = None,
    ) -> tuple[Goal | None, Instruction | None, str, list[dict[str, Any]]]:
        """LLM 및 등록된 Tool을 활용하여 사전 상태를 점검하고 조작 안내를 생성합니다.

        P0 안전 게이트(interpret_p0_goal)를 선행하여 비정상/미지원 목표의 임의 실행을 원천 차단합니다.
        """
        goal, clarification = self.interpret_goal(user_input, zone_id)
        if goal is None:
            return None, None, clarification or "지원하지 않는 목표입니다.", []

        act_id = action_id or f"act-{uuid.uuid4().hex[:6]}"
        fallback_instruction = self.build_instruction(goal, act_id)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"사용자 요청: '{user_input}'. "
                    f"선택 화구: '{goal.zone_id}'. "
                    "필요한 경우 센서 도구를 사용하여 사전 상태를 점검한 후, "
                    "사용자가 따라야 할 1문장의 안전한 물리 버튼 조작 안내를 생성해 주세요."
                ),
            },
        ]

        tool_schemas = self.tools.to_openai_tools() if self.tools else None
        executed_tool_calls: list[dict[str, Any]] = []
        self.executed_tool_calls = []

        instruction = fallback_instruction
        plan_message = "목표 해석 및 기본 조작 안내 생성"

        try:
            for _ in range(3):
                response = await self.llm.generate(messages, tools=tool_schemas)
                self.usage.record(response.usage, response.model)

                if response.tool_calls:
                    assistant_tool_calls = []
                    for tc in response.tool_calls:
                        assistant_tool_calls.append({
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                        })
                    messages.append({
                        "role": "assistant",
                        "content": response.text,
                        "tool_calls": assistant_tool_calls,
                    })

                    for tc in response.tool_calls:
                        if self.tools:
                            res = await self.tools.execute(tc.name, **tc.arguments)
                        else:
                            res = ToolResult(success=False, error="Tool registry가 없습니다.")
                        call_entry = {
                            "name": tc.name,
                            "arguments": tc.arguments,
                            "result": res.model_dump(),
                        }
                        executed_tool_calls.append(call_entry)
                        self.executed_tool_calls.append(call_entry)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(res.model_dump(), ensure_ascii=False),
                        })
                    continue

                if response.text and response.text.strip():
                    text = response.text.strip()
                    is_valid, validation_msg = validate_instruction_against_goal(text, goal)
                    if is_valid:
                        instruction = Instruction(action_id=act_id, text=text)
                        plan_message = "LLM 목표 분석 및 조작 안내 수립 완료"
                    else:
                        instruction = fallback_instruction
                        plan_message = f"LLM 조작 안내 안전 검증 불일치({validation_msg}), 안전 기본 안내 적용"
                    break
                raise LlmResponseFormatError("모델이 유효한 안내나 Tool 호출을 반환하지 않았습니다.")
            else:
                raise LlmResponseFormatError("모델 Tool 루프 한도(3회)를 소진했습니다.")
        except FATAL_LLM_ERRORS as exc:
            if getattr(exc, "usage", None) is not None:
                self.usage.record(exc.usage, exc.model)
            # 치명적 모델 오류(인증/한도/타임아웃/응답형식 등)는 fallback으로 감추지 않고 명시적으로 호출자에게 전파
            raise
        except Exception as exc:
            raise LlmProviderError(f"LLM 계획 호출 중단({type(exc).__name__}): {exc}") from exc

        return goal, instruction, plan_message, executed_tool_calls

    async def replan_recovery_with_llm(
        self,
        goal: Goal,
        recovery_result: RecoveryResult,
    ) -> tuple[Instruction | None, str]:
        """오조작 복구 결정(RecoveryResult)에 따라 LLM을 호출하여 명확한 재조작 안내를 생성합니다."""
        if (
            recovery_result.decision != RecoveryDecisionType.RETRY_INSTRUCTION
            or not recovery_result.instruction
        ):
            return recovery_result.instruction, "추가 조작을 중단하고 안전 상태를 확인해 주세요."

        fallback_inst = Instruction(
            action_id=recovery_result.instruction.action_id,
            text=instruction_text(goal, is_recovery=True),
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"이전 조작에서 출력 변화가 감지되지 않았습니다. 화구: {goal.zone_id}. "
                    "단 1회의 재조작 기회가 남았습니다. 사용자가 버튼을 다시 정확히 누를 수 있도록 "
                    "간결하고 명확한 1문장 조작 안내를 제시해 주세요."
                ),
            },
        ]

        try:
            response = await self.llm.generate(messages, tools=None)
            self.usage.record(response.usage, response.model)
            if response.tool_calls or not response.text or not response.text.strip():
                raise LlmResponseFormatError("모델이 유효한 복구 안내를 반환하지 않았습니다.")
            if response.text and response.text.strip():
                text = response.text.strip()
                is_valid, validation_msg = validate_instruction_against_goal(text, goal, is_recovery=True)
                if is_valid:
                    return (
                        Instruction(action_id=fallback_inst.action_id, text=text),
                        "LLM 재조작 안내 수립 완료",
                    )
                else:
                    return (
                        fallback_inst,
                        f"LLM 복구 안내 안전 검증 불일치({validation_msg}), 기본 재조작 안내 적용",
                    )
        except FATAL_LLM_ERRORS as exc:
            if getattr(exc, "usage", None) is not None:
                self.usage.record(exc.usage, exc.model)
            raise
        except Exception as exc:
            raise LlmProviderError(f"LLM 복구 호출 중단({type(exc).__name__}): {exc}") from exc


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

        # Keep only this action's immutable pre-observation baseline. A later frame
        # may become the next action's baseline, never this action's reference.
        key = (run_id, action_id, goal.zone_id, window_start_ms, window_end_ms)
        self._action_baselines = {key: baseline.model_copy(deep=True) if baseline else None}
        calibration = self.verifier.get_calibration_for_zone(goal.zone_id)
        self._action_profiles = {key: calibration}

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
            calibration=calibration,
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

        key = (run_id, action_id, goal.zone_id, window_start_ms, window_end_ms)
        baseline = self._action_baselines.pop(key, None)
        original_profile = self._action_profiles.pop(key, None)
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
                calibration=original_profile,
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
