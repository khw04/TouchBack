import json
from pathlib import Path
import pytest
from pydantic import ValidationError
from app.contracts.models import (
    AckRequest, AckResponse, CancelResponse, CreateRunRequest, CreateRunResponse,
    DeviceObservation, ErrorResponse, Goal, Observation, RunEvent, RunResponse, Verdict,
)

EXAMPLES = Path(__file__).resolve().parents[2] / "docs" / "examples"


def example(name):
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("model,name", [(DeviceObservation, "device-observation.json"), (Observation, "host-observation.json")])
def test_documented_examples_round_trip(model, name):
    parsed = model.model_validate(example(name))
    assert model.model_validate_json(parsed.model_dump_json()) == parsed


@pytest.mark.parametrize("key,value", [
    ("schema_version", "0.3"), ("seq", True), ("seq", "12"), ("seq", -1),
    ("device_id", ""), ("session_id", "   "), ("source", "simulation"),
    ("window_start_ms", -1), ("window_end_ms", 1200), ("beep_count", -1), ("scenario", "normal"),
])
def test_invalid_device_fields_rejected(key, value):
    payload = example("device-observation.json")
    payload[key] = value
    with pytest.raises(ValidationError):
        DeviceObservation.model_validate(payload)


@pytest.mark.parametrize("key,value", [("rms", float("nan")), ("rms", float("inf")), ("rms", -1.0), ("sample_count", 0), ("sample_rate_hz", False), ("unit", "tesla")])
def test_invalid_field_summary_rejected(key, value):
    payload = example("device-observation.json")
    payload["field_summary"][key] = value
    with pytest.raises(ValidationError):
        DeviceObservation.model_validate(payload)


def test_missing_is_distinct_from_zero_and_required_nullable_fields():
    payload = example("device-observation.json")
    payload.update(beep_count=None, field_summary=None)
    assert DeviceObservation.model_validate(payload).beep_count is None
    payload["beep_count"] = 0
    assert DeviceObservation.model_validate(payload).beep_count == 0
    del payload["beep_count"]
    with pytest.raises(ValidationError):
        DeviceObservation.model_validate(payload)


@pytest.mark.parametrize("changes", [
    {"observation_id": "other:boot:1"}, {"mapped_window_start_ms": None},
    {"mapped_window_end_ms": 501200}, {"received_monotonic_ms": -1},
    {"received_at": "2026-10-05T00:00:00"}, {"received_at": "2026-10-05T09:00:00+09:00"},
])
def test_invalid_host_metadata_rejected(changes):
    payload = example("host-observation.json")
    payload.update(changes)
    with pytest.raises(ValidationError):
        Observation.model_validate(payload)


def test_unmapped_window_preserves_unverified_clock():
    payload = example("host-observation.json")
    payload.update(mapped_window_start_ms=None, mapped_window_end_ms=None)
    assert Observation.model_validate(payload).clock_quality == "unverified"


@pytest.mark.parametrize("status,level,satisfied", [
    ("success", "exact_step", True), ("mismatch", "exact_step", False),
    ("mismatch", "none", False), ("uncertain", "output_change", None), ("uncertain", "none", None),
])
def test_documented_verdicts(status, level, satisfied):
    Verdict(status=status, verification_level=level, goal_satisfied=satisfied, reason_codes=[], evidence_ids=[])


@pytest.mark.parametrize("status,level,satisfied", [
    ("success", "output_change", True), ("success", "none", True),
    ("success", "exact_step", None), ("uncertain", "output_change", True), ("mismatch", "none", None),
])
def test_verdict_cannot_overclaim(status, level, satisfied):
    with pytest.raises(ValidationError):
        Verdict(status=status, verification_level=level, goal_satisfied=satisfied, reason_codes=[], evidence_ids=[])


@pytest.mark.parametrize("changes", [{"zone_id": "unknown"}, {"direction": "decrease"}, {"requested_steps": 2}, {"requested_steps": True}])
def test_goal_limited_to_current_scope(changes):
    payload = dict(zone_id="right", direction="increase", requested_steps=1)
    payload.update(changes)
    with pytest.raises(ValidationError):
        Goal.model_validate(payload)


def test_execution_contract_examples_round_trip():
    event = dict(event_id="event-1", run_id="run-1", action_id=None, timestamp="2026-10-10T00:00:00Z", kind="plan", source="mock", message="목표를 확인합니다.", evidence_ids=[])
    examples = [
        (Goal, dict(zone_id="right", direction="increase", requested_steps=1)),
        (CreateRunRequest, dict(user_input="화력 올려줘", source="mock", zone_id=None)),
        (CreateRunResponse, dict(run_id="run-1", status="planning", execution_mode="backend_stub")),
        (RunEvent, event),
        (RunResponse, dict(run_id="run-1", status="planning", execution_mode="backend_stub", source="mock", instruction=None, verdict=None, events=[event])),
        (AckRequest, dict(action_id="action-1")),
        (AckResponse, dict(run_id="run-1", status="observing", accepted=True)),
        (CancelResponse, dict(run_id="run-1", status="stopped")),
        (ErrorResponse, dict(code="busy", message="진행 중인 실행이 있습니다.")),
    ]
    for model, payload in examples:
        parsed = model.model_validate(payload)
        assert model.model_validate_json(parsed.model_dump_json()) == parsed


def test_ack_false_and_empty_input_rejected():
    with pytest.raises(ValidationError):
        AckResponse(run_id="run-1", status="observing", accepted=False)
    with pytest.raises(ValidationError):
        CreateRunRequest(user_input="   ", source="mock", zone_id=None)
