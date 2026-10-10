"""v0.2 data contracts; matching and verification belong to later tickets."""
from datetime import datetime, timedelta
from typing import Annotated, Literal
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

Text = Annotated[str, Field(min_length=1, pattern=r"\S")]
NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]
Source = Literal["mock", "live", "replay"]
Zone = Literal["right", "left"]
RunStatus = Literal["planning", "awaiting_clarification", "awaiting_user", "observing", "recovering", "succeeded", "uncertain", "stopped", "failed"]
ExecutionMode = Literal["backend_stub", "mock_llm", "real_llm"]


def parse_utc(value: object) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must have a UTC offset")
    return value


UtcTimestamp = Annotated[datetime, BeforeValidator(parse_utc)]


class Contract(BaseModel):
    # Do not coerce strings/bools into measurements or accept undeclared fields.
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


class FieldSummary(Contract):
    rms: Annotated[float, Field(ge=0)]
    unit: Literal["adc_count", "millivolt"]
    sample_count: PositiveInt
    sample_rate_hz: PositiveInt
    method: str


class Quality(Contract):
    status: Literal["valid", "degraded", "invalid"]
    reasons: list[str]


class DeviceObservation(Contract):
    schema_version: Literal["0.2"]
    device_id: Text
    session_id: Text
    seq: NonNegativeInt
    source: Source
    zone_id: Literal["right", "left", "unknown"]
    window_start_ms: NonNegativeInt
    window_end_ms: NonNegativeInt
    beep_count: NonNegativeInt | None
    field_summary: FieldSummary | None
    quality: Quality
    calibration_id: str | None

    @model_validator(mode="after")
    def ordered_window(self) -> "DeviceObservation":
        if self.window_start_ms >= self.window_end_ms:
            raise ValueError("device window must satisfy start < end")
        return self


class Observation(Contract):
    schema_version: Literal["0.2"]
    observation_id: Text
    run_id: Text | None
    action_id: Text | None
    received_at: UtcTimestamp
    received_monotonic_ms: NonNegativeInt
    mapped_window_start_ms: NonNegativeInt | None
    mapped_window_end_ms: NonNegativeInt | None
    clock_quality: Literal["verified", "unverified"]
    device_observation: DeviceObservation

    @model_validator(mode="after")
    def consistent_metadata(self) -> "Observation":
        d = self.device_observation
        if self.observation_id != f"{d.device_id}:{d.session_id}:{d.seq}":
            raise ValueError("observation_id must match device/session/seq")
        start, end = self.mapped_window_start_ms, self.mapped_window_end_ms
        if (start is None) != (end is None):
            raise ValueError("mapped endpoints must both be null or present")
        if start is not None and end is not None and start >= end:
            raise ValueError("mapped window must satisfy start < end")
        return self


class Goal(Contract):
    zone_id: Zone
    direction: Literal["increase"]
    requested_steps: Annotated[int, Field(ge=1, le=1)]


class Verdict(Contract):
    status: Literal["success", "mismatch", "uncertain"]
    verification_level: Literal["none", "output_change", "exact_step"]
    goal_satisfied: bool | None
    reason_codes: list[str]
    evidence_ids: list[str]

    @model_validator(mode="after")
    def consistent_verification(self) -> "Verdict":
        allowed = {
            ("success", "exact_step", True), ("mismatch", "exact_step", False),
            ("mismatch", "none", False), ("uncertain", "output_change", None),
            ("uncertain", "none", None),
        }
        if (self.status, self.verification_level, self.goal_satisfied) not in allowed:
            raise ValueError("verdict contradicts its verification level")
        return self


class RunEvent(Contract):
    event_id: Text
    run_id: Text
    action_id: Text | None
    timestamp: UtcTimestamp
    kind: Literal["plan", "instruction", "tool_call", "observation", "verdict", "recovery", "error"]
    source: Source
    message: str
    evidence_ids: list[str]


class CreateRunRequest(Contract):
    user_input: Text
    source: Source
    zone_id: Zone | None


class CreateRunResponse(Contract):
    run_id: Text
    status: RunStatus
    execution_mode: ExecutionMode


class Instruction(Contract):
    action_id: Text
    text: Text


class RunResponse(CreateRunResponse):
    source: Source
    instruction: Instruction | None
    verdict: Verdict | None
    events: list[RunEvent]


class AckRequest(Contract):
    action_id: Text


class AckResponse(Contract):
    run_id: Text
    status: RunStatus
    accepted: Literal[True]


class CancelResponse(Contract):
    run_id: Text
    status: RunStatus


class ErrorResponse(Contract):
    code: Literal["validation", "unknown_run", "busy", "stale_action", "terminal_run"]
    message: str
