"""Execution API (C-YD-00, C-YD-02, C-YD-03): run lifecycle, concurrency lock, timeouts, ack, cancel, and observation step."""
from datetime import datetime, timezone
import asyncio
import threading
import uuid
from typing import Callable, Literal

from fastapi import APIRouter, status
from pydantic import BaseModel

from app.agent.orchestrator import AgentOrchestrator
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

TERMINAL_STATUSES: set[RunStatus] = {"succeeded", "uncertain", "stopped", "failed"}
TIMEOUT_OBSERVING_SECONDS = 10
TIMEOUT_AWAITING_USER_SECONDS = 60
TIMEOUT_AWAITING_CLARIFICATION_SECONDS = 120


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

            # P1 피드백: 지원 목표 검증 (화력 1단 증가 외에는 + 조작 안내 금지)
            goal, clarification = interpret_p0_goal(req.user_input, req.zone_id)
            is_supported = goal is not None

            if not is_supported:
                initial_status: RunStatus = "awaiting_clarification"
                instruction: Instruction | None = None
                current_action_id: str | None = None
                seen_action_ids: list[str] = []
            else:
                initial_status = "awaiting_user"
                action_id = f"act-{uuid.uuid4().hex[:6]}"
                zone_label = "오른쪽" if goal.zone_id == "right" else "왼쪽"
                instruction = Instruction(
                    action_id=action_id,
                    text=f"{zone_label} 화구의 + 버튼을 한 번 눌러 주세요.",
                )
                current_action_id = action_id
                seen_action_ids = [action_id]

            state = RunState(
                run_id=run_id,
                status=initial_status,
                execution_mode="backend_stub",
                source=req.source,
                user_input=req.user_input,
                zone_id=goal.zone_id if goal else req.zone_id,
                instruction=instruction,
                current_action_id=current_action_id,
                seen_action_ids=seen_action_ids,
                retry_count=0,
                generation=1,
                created_at=now,
                status_updated_at=now,
            )

            # Record initial plan event
            self._add_event(
                state,
                kind="plan",
                message=f"목표 수신: {req.user_input} (출처: {req.source}, 화구: {req.zone_id or '미지정'})",
            )

            if not is_supported:
                self._add_event(
                    state,
                    kind="plan",
                    message=clarification,
                )
            else:
                self._add_event(
                    state,
                    kind="instruction",
                    message=instruction.text,
                    action_id=current_action_id,
                )

            self._runs[run_id] = state
            self._active_run_id = run_id

            return CreateRunResponse(
                run_id=run_id,
                status=state.status,
                execution_mode=state.execution_mode,
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

        orch = orchestrator or AgentOrchestrator()

        async def collect():
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
            return verdict, feedback, recovery

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
                    self._add_event(
                        current_state,
                        kind="recovery",
                        action_id=rec_res.instruction.action_id,
                        message=rec_res.message,
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
                evidence_ids=verdict.evidence_ids,
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
