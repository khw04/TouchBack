"""Execution API (C-YD-00, C-YD-02, C-YD-03, C-YD-04): run lifecycle, concurrency lock, timeouts, ack, cancel, observation step, and LLM/adapter integration."""
from datetime import datetime, timezone
import asyncio
import concurrent.futures
import threading
import uuid
from typing import Any, Callable, Coroutine, Literal

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict

from app.adapters.demo_setup import build_demo_adapter
from app.agent.orchestrator import FATAL_LLM_ERRORS, AgentOrchestrator
from app.agent.recovery import RecoveryDecisionType
from app.config import settings
from app.contracts.goal_policy import interpret_p0_goal
from app.contracts.models import (
    AckRequest,
    AckResponse,
    CancelResponse,
    CreateRunRequest,
    CreateRunResponse,
    ExecutionMode,
    Instruction,
    RunEvent,
    RunResponse,
    RunStatus,
    Source,
    Verdict,
    Zone,
)
from app.llm.factory import get_llm_provider
from app.tools.registry import create_registry_from_adapter, required_preflight

TERMINAL_STATUSES: set[RunStatus] = {"succeeded", "uncertain", "stopped", "failed"}
TIMEOUT_OBSERVING_SECONDS = 10
TIMEOUT_AWAITING_USER_SECONDS = 60
TIMEOUT_AWAITING_CLARIFICATION_SECONDS = 120


def run_sync(coro: Coroutine[Any, Any, Any], timeout: float = 10.0) -> Any:
    """Safely execute a coroutine from synchronous code with enforced timeout."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, asyncio.wait_for(coro, timeout=timeout)).result(timeout=timeout + 0.5)
    else:
        async def _runner():
            return await asyncio.wait_for(coro, timeout=timeout)
        return asyncio.run(_runner())


def resolve_adapter_for_run(state: "RunState") -> Any | None:
    """Return appropriate observation adapter based on run source and execution mode."""
    if state.source == "mock":
        if state.execution_mode == "backend_stub":
            # Keep unconfigured adapter behavior for backend_stub to preserve contract regression
            return None
        zone = state.zone_id if state.zone_id in ("right", "left") else "right"
        return build_demo_adapter(settings.mock_scenario, zone_id=zone)
    return None


class RunApiException(Exception):
    def __init__(
        self,
        status_code: int,
        code: Literal["validation", "unknown_run", "busy", "stale_action", "terminal_run"],
        message: str,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class StepRequest(AckRequest):
    """Bind observation requests to the acknowledged action, including retries."""


class RunState(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_id: str
    status: RunStatus
    execution_mode: ExecutionMode
    source: Source
    user_input: str
    zone_id: Zone | None
    instruction: Instruction | None = None
    verdict: Verdict | None = None
    events: list[RunEvent] = []
    current_action_id: str | None = None
    seen_action_ids: list[str] = []
    last_acked_action_id: str | None = None
    stepped_action_ids: list[str] = []
    retry_count: int = 0
    generation: int = 1
    action_window_start_ms: int = 500
    action_window_end_ms: int = 3500
    created_at: datetime
    status_updated_at: datetime
    adapter: Any | None = None


class RunStore:
    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, RunState] = {}
        self._active_run_id: str | None = None
        self._clock = clock

    def set_clock(self, clock: Callable[[], datetime] | None) -> None:
        with self._lock:
            self._clock = clock

    def reset(self) -> None:
        with self._lock:
            self._runs.clear()
            self._active_run_id = None
            self._clock = None

    def _now_utc(self) -> datetime:
        if self._clock is not None:
            return self._clock()
        return datetime.now(timezone.utc)

    def _add_event(
        self,
        state: RunState,
        kind: Literal["plan", "instruction", "tool_call", "observation", "verdict", "recovery", "error"],
        message: str,
        action_id: str | None = None,
        evidence_ids: list[str] | None = None,
    ) -> None:
        event = RunEvent(
            event_id=f"evt-{uuid.uuid4().hex[:8]}",
            run_id=state.run_id,
            action_id=action_id,
            timestamp=self._now_utc(),
            kind=kind,
            source=state.source,
            message=message,
            evidence_ids=evidence_ids or [],
        )
        state.events.append(event)

    def _check_and_expire(self, state: RunState, now: datetime) -> bool:
        """대기 시간 초과 만료 및 락 해제 (호출자는 self._lock을 보유해야 함)."""
        if state.status == "awaiting_user":
            elapsed = (now - state.status_updated_at).total_seconds()
            if elapsed >= TIMEOUT_AWAITING_USER_SECONDS:
                state.status = "stopped"
                state.status_updated_at = now
                state.generation += 1
                self._add_event(
                    state,
                    kind="plan",
                    message=f"사용자 조작 확인 대기 시간({TIMEOUT_AWAITING_USER_SECONDS}초) 초과로 실행이 자동 종료되었습니다.",
                )
                if self._active_run_id == state.run_id:
                    self._active_run_id = None
                return True
        elif state.status == "awaiting_clarification":
            elapsed = (now - state.status_updated_at).total_seconds()
            if elapsed >= TIMEOUT_AWAITING_CLARIFICATION_SECONDS:
                state.status = "stopped"
                state.status_updated_at = now
                state.generation += 1
                self._add_event(
                    state,
                    kind="plan",
                    message=f"화구/목표 명확화 대기 시간({TIMEOUT_AWAITING_CLARIFICATION_SECONDS}초) 초과로 실행이 자동 종료되었습니다.",
                )
                if self._active_run_id == state.run_id:
                    self._active_run_id = None
                return True
        elif state.status == "observing":
            if (now - state.status_updated_at).total_seconds() >= TIMEOUT_OBSERVING_SECONDS:
                self._finish_observation_error(state, "observation_timeout")
                return True
        return False

    def _finish_observation_error(self, state: RunState, reason: str) -> None:
        # Caller holds the store lock. Never expose exception text or fabricate sensor evidence.
        state.status = "uncertain"
        state.verdict = Verdict(status="uncertain", verification_level="none",
                                goal_satisfied=None, reason_codes=[reason], evidence_ids=[])
        state.status_updated_at = self._now_utc()
        state.generation += 1
        self._add_event(state, "error", "관측 결과를 확인할 수 없습니다. 추가 조작을 멈추고 인덕션 상태를 직접 확인해 주세요.", state.last_acked_action_id)
        if self._active_run_id == state.run_id:
            self._active_run_id = None

    def configure_mock_scenario(self, scenario: str) -> None:
        from app.adapters.demo_setup import SCENARIOS
        if scenario not in SCENARIOS:
            raise RunApiException(422, "validation", "지원하지 않는 모의 입력입니다.")
        with self._lock:
            if self._active_run_id is not None:
                state = self._runs.get(self._active_run_id)
                if state:
                    self._check_and_expire(state, self._now_utc())
                    if state.status not in TERMINAL_STATUSES:
                        raise RunApiException(409, "busy", "실행 중에는 모의 입력을 변경할 수 없습니다. 먼저 종료해 주세요.")
            settings.mock_scenario = scenario

    def create_run(self, req: CreateRunRequest) -> CreateRunResponse:
        with self._lock:
            now = self._now_utc()

            # P1 피드백: live 센서 어댑터 미준비 시 live 출처 거부
            if req.source == "live":
                raise RunApiException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    code="validation",
                    message="live 센서 연동이 준비되지 않았습니다. 현재는 mock 또는 replay 출처만 지원합니다.",
                )

            # 기존 활성 실행의 만료 검사 및 락 상태 원자적 확인
            if self._active_run_id is not None:
                active_run = self._runs.get(self._active_run_id)
                if active_run:
                    self._check_and_expire(active_run, now)
                    if active_run.status not in TERMINAL_STATUSES:
                        raise RunApiException(
                            status_code=status.HTTP_409_CONFLICT,
                            code="busy",
                            message="현재 진행 중인 다른 실행이 있습니다.",
                        )
                self._active_run_id = None

            run_id = f"run-{uuid.uuid4().hex[:8]}"
            exec_mode: ExecutionMode = settings.execution_mode

            # real_llm 모드에서 API 키가 누락된 경우 명시적 오류/대기 처리 (모의 모델로 속이지 않음)
            if exec_mode == "real_llm" and not settings.openai_api_key.strip():
                state = RunState(
                    run_id=run_id,
                    status="failed",
                    execution_mode="real_llm",
                    source=req.source,
                    user_input=req.user_input,
                    zone_id=req.zone_id,
                    instruction=None,
                    current_action_id=None,
                    seen_action_ids=[],
                    retry_count=0,
                    generation=1,
                    created_at=now,
                    status_updated_at=now,
                )
                self._add_event(
                    state,
                    kind="plan",
                    message=f"목표 수신: {req.user_input} (출처: {req.source}, 화구: {req.zone_id or '미지정'})",
                )
                self._add_event(
                    state,
                    kind="error",
                    message="OPENAI_API_KEY가 설정되지 않아 실제 LLM을 호출할 수 없습니다 (provider_waiting). 환경설정을 확인해 주세요.",
                )
                self._runs[run_id] = state
                self._active_run_id = None
                return CreateRunResponse(
                    run_id=run_id,
                    status=state.status,
                    execution_mode=state.execution_mode,
                )

            # P1 피드백: 지원 목표 검증 (화력 1단 증가 외에는 + 조작 안내 금지)
            goal, clarification = interpret_p0_goal(req.user_input, req.zone_id)
            is_supported = goal is not None

            if not is_supported:
                state = RunState(
                    run_id=run_id,
                    status="awaiting_clarification",
                    execution_mode=exec_mode,
                    source=req.source,
                    user_input=req.user_input,
                    zone_id=req.zone_id,
                    instruction=None,
                    current_action_id=None,
                    seen_action_ids=[],
                    retry_count=0,
                    generation=1,
                    created_at=now,
                    status_updated_at=now,
                )
                self._add_event(
                    state,
                    kind="plan",
                    message=f"목표 수신: {req.user_input} (출처: {req.source}, 화구: {req.zone_id or '미지정'})",
                )
                self._add_event(
                    state,
                    kind="plan",
                    message=clarification or "지원하지 않는 목표입니다.",
                )
                self._runs[run_id] = state
                self._active_run_id = run_id
                return CreateRunResponse(
                    run_id=run_id,
                    status=state.status,
                    execution_mode=state.execution_mode,
                )

            # 지원 목표인 경우: planning 상태를 먼저 원자적으로 예약
            action_id = f"act-{uuid.uuid4().hex[:6]}"
            temp_state = RunState(
                run_id=run_id,
                status="planning",
                execution_mode=exec_mode,
                source=req.source,
                user_input=req.user_input,
                zone_id=goal.zone_id,
                created_at=now,
                status_updated_at=now,
            )
            adapter = resolve_adapter_for_run(temp_state)
            temp_state.adapter = adapter

            state = temp_state
            self._runs[run_id] = state
            self._active_run_id = run_id
            expected_generation = state.generation

            self._add_event(
                state,
                kind="plan",
                message=f"목표 수신: {req.user_input} (출처: {req.source}, 화구: {req.zone_id or '미지정'})",
            )

        # 저장소 잠금(self._lock) 밖에서 실제 시간 제한(10.0초)을 적용하여 LLM 계획 수행
        executed_tool_calls: list[dict[str, Any]] = []
        plan_detail_msg: str | None = None
        usage_msg: str | None = None
        instruction: Instruction | None = None
        planning_error: Exception | None = None

        if exec_mode in ("mock_llm", "real_llm"):
            provider = get_llm_provider(mode=exec_mode)
            tool_registry = create_registry_from_adapter(
                adapter, source=req.source, run_id=run_id, zone_id=goal.zone_id, for_planning=True
            )
            orch = AgentOrchestrator(llm_provider=provider, adapter=adapter, tool_registry=tool_registry)

            async def plan_with_required_checks():
                planned_goal, planned_instruction, message, _ = await orch.plan_and_instruct(
                    req.user_input, goal.zone_id, action_id
                )
                # These checks are server policy, independent of model Tool selection.
                # Run immediately before publishing an actionable instruction.
                orch.executed_tool_calls.extend(await required_preflight(tool_registry, goal.zone_id))
                return planned_goal, planned_instruction, message, list(orch.executed_tool_calls)

            try:
                _, instruction, plan_detail_msg, executed_tool_calls = run_sync(
                    plan_with_required_checks(),
                    timeout=10.0,
                )
            except Exception as exc:
                planning_error = exc
            finally:
                if not executed_tool_calls and getattr(orch, "executed_tool_calls", None):
                    executed_tool_calls = list(orch.executed_tool_calls)
                if orch.usage.call_count > 0:
                    usage_msg = orch.usage.to_event_message()
        else:
            zone_label = "오른쪽" if goal.zone_id == "right" else "왼쪽"
            instruction = Instruction(
                action_id=action_id,
                text=f"{zone_label} 화구의 + 버튼을 한 번 눌러 주세요.",
            )
            plan_detail_msg = "기본 조작 안내 적용"

        # 계획 완료 후 저장소 잠금을 다시 획득하고 원자적으로 결과 게시
        with self._lock:
            current_state = self._runs.get(run_id)
            if not current_state:
                raise RunApiException(404, "unknown_run", f"실행 '{run_id}'을(를) 찾을 수 없습니다.")

            now_after_plan = self._now_utc()
            self._check_and_expire(current_state, now_after_plan)

            # 취소(cancel) 또는 대기 만료 등으로 상태/세대가 변경된 경우 계획 결과를 덮어쓰지 않음
            if (
                current_state.status != "planning"
                or current_state.generation != expected_generation
                or self._active_run_id != run_id
            ):
                return CreateRunResponse(
                    run_id=current_state.run_id,
                    status=current_state.status,
                    execution_mode=current_state.execution_mode,
                )

            # 1. 사전 점검 도구 실행 결과 기록 및 센서 결함 여부 확인 (실패/센서이상/정상 모두 공통 보존)
            sensor_fault_detected = False
            sensor_fault_reason = ""
            for tc in executed_tool_calls:
                tool_name = tc.get("name", "unknown")
                tool_result = tc.get("result", {})
                tool_success = tool_result.get("success", False)
                if tool_success:
                    self._add_event(
                        current_state,
                        kind="tool_call",
                        message=f"도구 '{tool_name}' 실행 완료",
                        action_id=action_id,
                    )
                else:
                    err_text = tool_result.get("error", "도구 실행 실패")
                    self._add_event(
                        current_state,
                        kind="tool_call",
                        message=f"도구 '{tool_name}' 실행 실패: {err_text}",
                        action_id=action_id,
                    )
                    if tool_name in ("check_sensor_integrity", "read_field"):
                        sensor_fault_detected = True
                        sensor_fault_reason = err_text

            # 2. 모델 토큰 사용량 기록 (실패/센서이상/정상 모두 공통 보존)
            if usage_msg:
                self._add_event(
                    current_state,
                    kind="plan",
                    message=f"LLM 계획 토큰 메트릭: {usage_msg}",
                    action_id=action_id,
                )

            # 3. 처리 불가능한 치명적 모델 오류(인증/한도/타임아웃/응답형식 등) 발생 시 명시적 실패(failed) 종료
            if planning_error is not None:
                current_state.status = "failed"
                current_state.instruction = None
                current_state.current_action_id = None
                current_state.status_updated_at = now_after_plan
                current_state.generation += 1
                self._add_event(
                    current_state,
                    kind="error",
                    message=f"LLM 계획 수립 실패({type(planning_error).__name__}): {planning_error}",
                    action_id=action_id,
                )
                if self._active_run_id == run_id:
                    self._active_run_id = None
                return CreateRunResponse(
                    run_id=current_state.run_id,
                    status=current_state.status,
                    execution_mode=current_state.execution_mode,
                )

            # 4. 사전 점검 센서 결함 감지 시 명시적 uncertain 종료
            if sensor_fault_detected:
                current_state.status = "uncertain"
                current_state.verdict = Verdict(
                    status="uncertain",
                    verification_level="none",
                    goal_satisfied=None,
                    reason_codes=["sensor_integrity_check_failed"],
                    evidence_ids=[],
                )
                current_state.instruction = None
                current_state.current_action_id = None
                current_state.status_updated_at = now_after_plan
                current_state.generation += 1
                self._add_event(
                    current_state,
                    kind="error",
                    message=f"센서 사전 점검에서 이상이 감지되었습니다: {sensor_fault_reason}. 조작 안내를 중단합니다.",
                    action_id=action_id,
                )
                if self._active_run_id == run_id:
                    self._active_run_id = None
                return CreateRunResponse(
                    run_id=current_state.run_id,
                    status=current_state.status,
                    execution_mode=current_state.execution_mode,
                )

            # 5. 정상: 안내 게시 및 사용자 조작 대기(awaiting_user) 전이
            current_state.status = "awaiting_user"
            current_state.instruction = instruction
            current_state.current_action_id = action_id
            current_state.seen_action_ids = [action_id]
            current_state.status_updated_at = now_after_plan  # 안내 게시 시점으로 갱신
            current_state.generation += 1

            if plan_detail_msg:
                self._add_event(
                    current_state,
                    kind="plan",
                    message=plan_detail_msg,
                    action_id=action_id,
                )
            if instruction:
                self._add_event(
                    current_state,
                    kind="instruction",
                    message=instruction.text,
                    action_id=action_id,
                )

            return CreateRunResponse(
                run_id=current_state.run_id,
                status=current_state.status,
                execution_mode=current_state.execution_mode,
            )

    @staticmethod
    def get_snapshot(state: RunState) -> RunResponse:
        return RunResponse(run_id=state.run_id, status=state.status,
                           execution_mode=state.execution_mode, source=state.source,
                           instruction=state.instruction, verdict=state.verdict,
                           events=state.events).model_copy(deep=True)

    def get_run(self, run_id: str) -> RunResponse:
        with self._lock:
            state = self._runs.get(run_id)
            if not state:
                raise RunApiException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    code="unknown_run",
                    message=f"실행 '{run_id}'을(를) 찾을 수 없습니다.",
                )
            self._check_and_expire(state, self._now_utc())
            return RunResponse(
                run_id=state.run_id,
                status=state.status,
                execution_mode=state.execution_mode,
                source=state.source,
                instruction=state.instruction,
                verdict=state.verdict,
                events=state.events,
            )

    def ack_run(self, run_id: str, req: AckRequest) -> AckResponse:
        with self._lock:
            state = self._runs.get(run_id)
            if not state:
                raise RunApiException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    code="unknown_run",
                    message=f"실행 '{run_id}'을(를) 찾을 수 없습니다.",
                )

            now = self._now_utc()
            self._check_and_expire(state, now)

            if state.status in TERMINAL_STATUSES:
                if req.action_id == state.last_acked_action_id:
                    return AckResponse(run_id=state.run_id, status=state.status, accepted=True)
                raise RunApiException(
                    status_code=status.HTTP_409_CONFLICT,
                    code="terminal_run",
                    message="이미 종료된 실행입니다.",
                )

            if req.action_id == state.current_action_id:
                if state.last_acked_action_id != req.action_id:
                    state.status = "observing"
                    state.status_updated_at = now
                    state.last_acked_action_id = req.action_id
                    state.generation += 1
                    self._add_event(
                        state,
                        kind="observation",
                        action_id=req.action_id,
                        message="조작 완료 확인 수신. 센서 관측을 대기합니다.",
                    )
                return AckResponse(run_id=state.run_id, status=state.status, accepted=True)

            if req.action_id in state.seen_action_ids:
                raise RunApiException(
                    status_code=status.HTTP_409_CONFLICT,
                    code="stale_action",
                    message="이전 조작의 완료 알림입니다.",
                )

            raise RunApiException(
                status_code=status.HTTP_409_CONFLICT,
                code="stale_action",
                message="유효하지 않거나 일치하지 않는 조작 ID입니다.",
            )

    def cancel_run(self, run_id: str) -> CancelResponse:
        with self._lock:
            state = self._runs.get(run_id)
            if not state:
                raise RunApiException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    code="unknown_run",
                    message=f"실행 '{run_id}'을(를) 찾을 수 없습니다.",
                )

            now = self._now_utc()
            self._check_and_expire(state, now)
            if state.status in TERMINAL_STATUSES:
                return CancelResponse(run_id=state.run_id, status=state.status)

            state.status = "stopped"
            state.status_updated_at = now
            state.generation += 1
            self._add_event(
                state,
                kind="plan",
                message="실행이 취소되었습니다. 현재 인덕션 안전 상태를 직접 확인해 주세요.",
            )

            if self._active_run_id == run_id:
                self._active_run_id = None

            return CancelResponse(run_id=state.run_id, status="stopped")

    async def execute_observation_step(
        self,
        run_id: str,
        orchestrator: AgentOrchestrator | None = None,
        action_id: str | None = None,
    ) -> RunResponse:
        """observing 상태의 실행에 대해 관측 수집 및 의도 검증 사이클 1회를 원자적으로 수행합니다."""
        with self._lock:
            state = self._runs.get(run_id)
            if not state:
                raise RunApiException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    code="unknown_run",
                    message=f"실행 '{run_id}'을(를) 찾을 수 없습니다.",
                )

            self._check_and_expire(state, self._now_utc())
            requested_action = action_id or state.last_acked_action_id
            if requested_action in state.stepped_action_ids:
                return self.get_snapshot(state)
            if state.status in TERMINAL_STATUSES:
                return self.get_snapshot(state)
            if state.status != "observing":
                raise RunApiException(
                    status_code=status.HTTP_409_CONFLICT,
                    code="terminal_run" if state.status in TERMINAL_STATUSES else "stale_action",
                    message=f"현재 상태('{state.status}')에서는 관측을 진행할 수 없습니다. observing 상태여야 합니다.",
                )

            if requested_action != state.last_acked_action_id:
                raise RunApiException(409, "stale_action", "현재 완료 확인된 조작 ID가 아닙니다.")
            action_id = requested_action
            state.stepped_action_ids.append(action_id)
            goal, _ = interpret_p0_goal(state.user_input, state.zone_id)
            if goal is None:
                self._finish_observation_error(state, "goal_validation_failed")
                return self.get_snapshot(state)
            expected_generation = state.generation
            expected_action_id = action_id
            window_start_ms = state.action_window_start_ms
            window_end_ms = state.action_window_end_ms
            retry_count = state.retry_count

        if orchestrator is not None:
            orch = orchestrator
        else:
            adapter = state.adapter or resolve_adapter_for_run(state)
            provider = get_llm_provider(mode=state.execution_mode)
            tool_registry = create_registry_from_adapter(
                adapter, source=state.source, run_id=run_id, zone_id=goal.zone_id,
            )
            orch = AgentOrchestrator(llm_provider=provider, adapter=adapter, tool_registry=tool_registry)

        recovery_llm_error: Exception | None = None
        recovery_checks: list[dict[str, Any]] = []

        async def collect_result():
            nonlocal recovery_llm_error, recovery_checks
            verdict, feedback = await orch.execute_observation_and_verification(
                goal=goal, action_id=action_id, run_id=run_id,
                window_start_ms=window_start_ms, window_end_ms=window_end_ms,
            )
            recovery = None
            with self._lock:
                current = self._runs.get(run_id)
                if current:
                    self._check_and_expire(current, self._now_utc())
                valid = current and current.status == "observing" and current.generation == expected_generation
            if not valid:
                return verdict, feedback, None
            if orch.determine_next_status(verdict) == "recovering":
                recovery = orch.handle_recovery(
                    goal=goal, verdict=verdict, retry_count=retry_count,
                    run_id=run_id, action_id=action_id,
                    window_start_ms=window_start_ms, window_end_ms=window_end_ms,
                )
                if recovery.decision.value == "retry_instruction":
                    try:
                        inst, plan_msg = await orch.replan_recovery_with_llm(goal, recovery)
                    except FATAL_LLM_ERRORS as exc:
                        recovery_llm_error = exc
                        raise
                    recovery.instruction = inst
                    recovery.message = inst.text if inst else ""
                    recovery.plan_message = plan_msg
                    registry = create_registry_from_adapter(
                        orch.adapter, source=state.source, run_id=run_id,
                        zone_id=goal.zone_id, for_planning=True,
                        calibration=orch.verifier.get_calibration_for_zone(goal.zone_id),
                    )
                    recovery_checks = await required_preflight(registry, goal.zone_id)
                    if any(not call["result"]["success"] for call in recovery_checks):
                        recovery.decision = RecoveryDecisionType.ABORT_UNCERTAIN
                        recovery.instruction = None
                        recovery.message = "센서 사전 점검에서 이상이 감지되었습니다. 추가 조작을 중단합니다."
                        recovery.updated_verdict = Verdict(
                            status="uncertain", verification_level="none", goal_satisfied=None,
                            reason_codes=["sensor_integrity_check_failed"], evidence_ids=verdict.evidence_ids,
                        )
            return verdict, feedback, recovery

        async def collect():
            try:
                return await collect_result()
            finally:
                # Billing facts belong to this worker, independent of state publication.
                # This also runs if the caller timed out while the worker was still waiting.
                if orch.usage.call_count > 0:
                    with self._lock:
                        current = self._runs.get(run_id)
                        if current is state:
                            self._add_event(current, "plan", f"LLM 복구 토큰 메트릭: {orch.usage.to_event_message()}", action_id)

        try:
            # Sensor adapters are synchronous. Keep their waits outside the event loop and store lock.
            remaining = max(0, TIMEOUT_OBSERVING_SECONDS -
                            (self._now_utc() - state.status_updated_at).total_seconds())
            verdict, feedback, rec_res = await asyncio.wait_for(
                asyncio.to_thread(lambda: asyncio.run(collect())), timeout=remaining,
            )
        except (Exception, asyncio.CancelledError) as exc:
            with self._lock:
                current = self._runs.get(run_id)
                if current and current.status == "observing" and current.generation == expected_generation:
                    reason = "observation_timeout" if isinstance(exc, TimeoutError) else "observation_processing_error"
                    self._finish_observation_error(current, reason)
                    if recovery_llm_error is not None:
                        current.status = "failed"
                        current.instruction = None
                        current.current_action_id = None
                        self._add_event(current, "error", f"LLM 복구 안내 수립 실패({type(exc).__name__}). 추가 조작을 중단합니다.", action_id)
                snapshot = self.get_snapshot(current) if current else None
            if isinstance(exc, asyncio.CancelledError):
                raise
            if snapshot is None:
                raise RunApiException(404, "unknown_run", "실행을 찾을 수 없습니다.")
            return snapshot

        with self._lock:
            current_state = self._runs.get(run_id)
            if not current_state:
                raise RunApiException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    code="unknown_run",
                    message=f"실행 '{run_id}'을(를) 찾을 수 없습니다.",
                )

            self._check_and_expire(current_state, self._now_utc())

            # 원자적 세대 및 상태 유효성 재확인:
            # await 도중 취소(stopped), 만료, 다른 조작 진행 등으로 세대나 상태가 변경되었으면
            # 늦은 관측 결과를 무시하고 현재 상태를 덮어쓰지 않는다.
            if (
                current_state.status != "observing"
                or current_state.generation != expected_generation
                or current_state.last_acked_action_id != expected_action_id
                or self._active_run_id != run_id
            ):
                return RunResponse(
                    run_id=current_state.run_id,
                    status=current_state.status,
                    execution_mode=current_state.execution_mode,
                    source=current_state.source,
                    instruction=current_state.instruction,
                    verdict=current_state.verdict,
                    events=current_state.events,
                )

            now = self._now_utc()
            current_state.verdict = verdict
            next_status = orch.determine_next_status(verdict)
            for call in recovery_checks:
                result = call["result"]
                detail = "실행 완료" if result["success"] else f"실행 실패: {result['error']}"
                self._add_event(current_state, "tool_call", f"필수 사전 점검 도구 '{call['name']}' {detail}", action_id)

            # C-YD-03 오조작 복구 Skill 연계:
            # 유효한 변화 없음(mismatch/none/no_output_change)인 경우에만 recovering 진입
            if next_status == "recovering":
                if rec_res.decision.value == "retry_instruction" and rec_res.instruction is not None:
                    current_state.retry_count += 1
                    current_state.status = "awaiting_user"
                    current_state.instruction = rec_res.instruction
                    current_state.current_action_id = rec_res.instruction.action_id
                    current_state.seen_action_ids.append(rec_res.instruction.action_id)
                    current_state.action_window_start_ms = rec_res.next_window_start_ms
                    current_state.action_window_end_ms = rec_res.next_window_end_ms
                    current_state.status_updated_at = now
                    current_state.generation += 1

                    self._add_event(
                        current_state,
                        kind="observation",
                        action_id=action_id,
                        message="관측 처리 종료. 판정 수준과 근거를 확인해 주세요.",
                        evidence_ids=verdict.evidence_ids,
                    )
                    plan_detail = getattr(rec_res, "plan_message", None) or "오조작 복구: 재조작 안내 수립"
                    self._add_event(
                        current_state,
                        kind="plan",
                        action_id=rec_res.instruction.action_id,
                        message=plan_detail,
                    )
                    self._add_event(
                        current_state,
                        kind="recovery",
                        action_id=rec_res.instruction.action_id,
                        message=rec_res.instruction.text,
                        evidence_ids=verdict.evidence_ids,
                    )
                    return RunResponse(
                        run_id=current_state.run_id,
                        status=current_state.status,
                        execution_mode=current_state.execution_mode,
                        source=current_state.source,
                        instruction=current_state.instruction,
                        verdict=current_state.verdict,
                        events=current_state.events,
                    )
                elif rec_res.decision.value == "late_success":
                    next_status = "succeeded"
                    if rec_res.updated_verdict:
                        current_state.verdict = rec_res.updated_verdict
                    feedback = rec_res.message
                else:  # abort_uncertain
                    next_status = "uncertain"
                    current_state.instruction = None
                    current_state.current_action_id = None
                    if rec_res.updated_verdict:
                        current_state.verdict = rec_res.updated_verdict
                    feedback = rec_res.message

            current_state.status = next_status
            current_state.status_updated_at = now
            current_state.generation += 1

            self._add_event(
                current_state,
                kind="observation",
                action_id=action_id,
                message="관측 처리 종료. 판정 수준과 근거를 확인해 주세요.",
                evidence_ids=verdict.evidence_ids,
            )
            self._add_event(
                current_state,
                kind="verdict",
                action_id=action_id,
                message=feedback,
                evidence_ids=current_state.verdict.evidence_ids,
            )

            if next_status in TERMINAL_STATUSES and self._active_run_id == run_id:
                self._active_run_id = None

            return RunResponse(
                run_id=current_state.run_id,
                status=current_state.status,
                execution_mode=current_state.execution_mode,
                source=current_state.source,
                instruction=current_state.instruction,
                verdict=current_state.verdict,
                events=current_state.events,
            )


run_store = RunStore()
router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=CreateRunResponse)
def create_run(req: CreateRunRequest) -> CreateRunResponse:
    return run_store.create_run(req)


@router.get("/{run_id}", response_model=RunResponse)
def get_run(run_id: str) -> RunResponse:
    return run_store.get_run(run_id)


@router.post("/{run_id}/ack", response_model=AckResponse)
def ack_run(run_id: str, req: AckRequest) -> AckResponse:
    return run_store.ack_run(run_id, req)


@router.post("/{run_id}/step", response_model=RunResponse)
async def step_run(run_id: str, req: StepRequest) -> RunResponse:
    return await run_store.execute_observation_step(run_id, action_id=req.action_id)


@router.post("/{run_id}/cancel", response_model=CancelResponse)
def cancel_run(run_id: str) -> CancelResponse:
    return run_store.cancel_run(run_id)
