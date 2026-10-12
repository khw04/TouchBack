"""Execution API (C-YD-00): run lifecycle, concurrency lock, timeouts, ack, and cancel."""
from datetime import datetime, timezone
import threading
import re
import uuid
from typing import Callable, Literal

from fastapi import APIRouter, status
from pydantic import BaseModel

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


def is_supported_p0_goal(user_input: str) -> bool:
    """P0 지원 목표 여부 검증 (화력 1단 증가만 지원, 감소/종료는 미지원)."""
    text = user_input.lower().strip()
    # 부정/금지 및 P0 범위 밖 단계 요청은 실제 조작으로 변환하지 않는다.
    if re.search(r"(?:하지|주지|리지|시키지|지)\s*마|말아|말고|않|안\s*(?:올|높|켜|시작|증가)", text):
        return False
    if re.search(r"(?:[02-9]\d*|1\d+)\s*(?:단|단계)|(?:두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*(?:단계|단|번)", text):
        return False
    unsupported_keywords = [
        "꺼",
        "종료",
        "꺼줘",
        "내려",
        "감소",
        "낮춰",
        "줄여",
        "정지",
        "차단",
        "off",
        "중단",
    ]
    for kw in unsupported_keywords:
        if kw in text:
            return False

    supported_keywords = [
        "올려",
        "증가",
        "높여",
        "키워",
        "1단",
        "한 단계",
        "켜",
        "시작",
        "+",
    ]
    return any(kw in text for kw in supported_keywords)


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
                self._add_event(
                    state,
                    kind="plan",
                    message=f"화구/목표 명확화 대기 시간({TIMEOUT_AWAITING_CLARIFICATION_SECONDS}초) 초과로 실행이 자동 종료되었습니다.",
                )
                if self._active_run_id == state.run_id:
                    self._active_run_id = None
                return True
        return False

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
            is_supported = is_supported_p0_goal(req.user_input)

            if not is_supported:
                initial_status: RunStatus = "awaiting_clarification"
                instruction: Instruction | None = None
                current_action_id: str | None = None
                seen_action_ids: list[str] = []
            elif req.zone_id is None:
                initial_status = "awaiting_clarification"
                instruction = None
                current_action_id = None
                seen_action_ids = []
            else:
                initial_status = "awaiting_user"
                action_id = f"act-{uuid.uuid4().hex[:6]}"
                zone_label = "오른쪽" if req.zone_id == "right" else "왼쪽"
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
                zone_id=req.zone_id,
                instruction=instruction,
                current_action_id=current_action_id,
                seen_action_ids=seen_action_ids,
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
                    message=f"지원하지 않거나 준비되지 않은 목표입니다: '{req.user_input}'. P0에서는 화력 1단 증가('화력 1단 올려줘')만 지원합니다.",
                )
            elif req.zone_id is None:
                self._add_event(
                    state,
                    kind="plan",
                    message="화구가 지정되지 않았습니다. 조작할 화구를 선택해 주세요.",
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
            if state.status == "stopped":
                return CancelResponse(run_id=state.run_id, status="stopped")

            state.status = "stopped"
            state.status_updated_at = now
            self._add_event(
                state,
                kind="plan",
                message="실행이 취소되었습니다. 현재 인덕션 안전 상태를 직접 확인해 주세요.",
            )

            if self._active_run_id == run_id:
                self._active_run_id = None

            return CancelResponse(run_id=state.run_id, status="stopped")


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


@router.post("/{run_id}/cancel", response_model=CancelResponse)
def cancel_run(run_id: str) -> CancelResponse:
    return run_store.cancel_run(run_id)
