import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.adapters.mock_setup import build_mock_adapter
from app.contracts.models import Goal
from app.verification.models import ActionContext, MockCalibration
from app.verification.verifier import verify_intent

PROFILE = Path(__file__).resolve().parents[1] / "app/verification/mock-calibration.json"


def setup(scenario="normal"):
    adapter = build_mock_adapter(scenario)
    baseline = adapter.read_baseline("right")
    observations = adapter.await_touch_result(run_id="run-1", action_id="action-1", zone_id="right", window_start_ms=500, window_end_ms=3500)
    goal = Goal(zone_id="right", direction="increase", requested_steps=1)
    context = ActionContext(run_id="run-1", action_id="action-1", window_start_ms=500, window_end_ms=3500, now_ms=3500)
    calibration = MockCalibration.model_validate_json(PROFILE.read_text(encoding="utf-8"))
    return goal, baseline, observations, context, calibration


def test_normal_mock_is_exact_step_with_evidence():
    args = setup()
    result = verify_intent(*args)
    assert (result.status, result.verification_level, result.goal_satisfied) == ("success", "exact_step", True)
    assert result.evidence_ids == [args[1].observation_id, args[2][0].observation_id]


@pytest.mark.parametrize("scenario,status,level,reason", [
    ("no_change", "mismatch", "none", "no_output_change"),
    ("missing", "uncertain", "none", "observations_missing"),
    ("sensor_error", "uncertain", "none", "sensor_quality_invalid"),
])
def test_mock_error_and_no_change_paths(scenario, status, level, reason):
    result = verify_intent(*setup(scenario))
    assert (result.status, result.verification_level) == (status, level)
    assert reason in result.reason_codes


def test_output_change_does_not_become_exact_step_success():
    args = setup()
    args[2][0].device_observation.field_summary.rms = 130.0
    result = verify_intent(*args)
    assert (result.status, result.verification_level, result.goal_satisfied) == ("uncertain", "output_change", None)


def test_calibrated_two_steps_is_mismatch():
    args = setup()
    args[2][0].device_observation.field_summary.rms = 140.0
    result = verify_intent(*args)
    assert (result.status, result.verification_level, result.goal_satisfied) == ("mismatch", "exact_step", False)


def test_beep_alone_cannot_prove_success():
    args = setup()
    args[2][0].device_observation.field_summary = None
    args[2][0].device_observation.beep_count = 10
    assert verify_intent(*args).status == "uncertain"


def test_beep_is_not_required_for_field_verification():
    args = setup()
    args[2][0].device_observation.beep_count = None
    assert verify_intent(*args).status == "success"


@pytest.mark.parametrize("field,value,reason", [
    ("clock_quality", "unverified", "clock_unverified"),
    ("mapped_window_start_ms", 499, "outside_action_window"),
    ("mapped_window_end_ms", 3501, "outside_action_window"),
    ("received_monotonic_ms", 3501, "invalid_received_time"),
    ("received_monotonic_ms", 1000, "invalid_received_time"),
    ("run_id", "other-run", "action_mismatch"),
    ("action_id", "old-action", "action_mismatch"),
])
def test_host_evidence_failures(field, value, reason):
    args = setup()
    setattr(args[2][0], field, value)
    result = verify_intent(*args)
    assert result.status == "uncertain"
    assert reason in result.reason_codes


@pytest.mark.parametrize("field,value,reason", [
    ("source", "live", "source_mismatch"), ("source", "replay", "source_mismatch"),
    ("zone_id", "left", "zone_mismatch"),
    ("calibration_id", None, "calibration_mismatch"),
    ("calibration_id", "other", "calibration_mismatch"),
])
def test_device_scope_failures(field, value, reason):
    args = setup()
    setattr(args[2][0].device_observation, field, value)
    result = verify_intent(*args)
    assert result.status == "uncertain"
    assert reason in result.reason_codes


def test_reboot_invalidates_baseline_and_calibration_scope():
    args = setup()
    item = args[2][0]
    item.device_observation.session_id = "reboot-2"
    item.observation_id = "mock-device-01:reboot-2:5"
    assert "device_session_mismatch" in verify_intent(*args).reason_codes


@pytest.mark.parametrize("field,value", [("unit", "millivolt"), ("method", "envelope_v1"), ("sample_rate_hz", 800)])
def test_measurement_settings_are_not_interchangeable(field, value):
    args = setup()
    setattr(args[2][0].device_observation.field_summary, field, value)
    assert "measurement_settings_mismatch" in verify_intent(*args).reason_codes


def test_quality_reasons_cannot_hide_under_valid_status():
    args = setup()
    args[2][0].device_observation.quality.reasons = ["saturated"]
    assert "sensor_quality_invalid" in verify_intent(*args).reason_codes


def test_calibration_and_baseline_are_required():
    goal, baseline, observations, context, calibration = setup()
    assert "calibration_missing" in verify_intent(goal, baseline, observations, context, None).reason_codes
    assert "baseline_missing" in verify_intent(goal, None, observations, context, calibration).reason_codes


def test_baseline_must_be_fresh_and_received_before_instruction():
    args = setup()
    args[4].max_baseline_age_ms = 0
    args[1].mapped_window_end_ms = 499
    assert "baseline_stale" in verify_intent(*args).reason_codes
    args = setup()
    args[1].received_monotonic_ms = 501
    assert "baseline_after_instruction" in verify_intent(*args).reason_codes


def test_baseline_outside_validated_operating_range_is_uncertain():
    args = setup()
    args[1].device_observation.field_summary.rms = 200.0
    assert "baseline_outside_calibration" in verify_intent(*args).reason_codes


def test_stale_followup_and_transport_delay_are_rejected():
    args = setup()
    args[3].now_ms = 7000
    assert "observation_stale" in verify_intent(*args).reason_codes
    args = setup()
    args[4].max_transport_delay_ms = 0
    args[2][0].received_monotonic_ms += 1
    assert "transport_delay_exceeded" in verify_intent(*args).reason_codes


def test_short_or_incomplete_sampling_is_uncertain():
    args = setup()
    args[2][0].mapped_window_start_ms = 1100
    assert "observation_window_too_short" in verify_intent(*args).reason_codes
    args = setup()
    args[2][0].device_observation.field_summary.sample_count = 1
    assert "insufficient_samples" in verify_intent(*args).reason_codes


def test_identical_duplicate_is_deduplicated_but_conflict_is_rejected():
    args = setup()
    args[2].append(args[2][0].model_copy(deep=True))
    result = verify_intent(*args)
    assert result.status == "success" and len(result.evidence_ids) == 2
    args[2][1].device_observation.beep_count = 2
    assert "conflicting_duplicate" in verify_intent(*args).reason_codes


def test_conflicting_or_invalid_later_evidence_blocks_success():
    args = setup()
    other = args[2][0].model_copy(deep=True)
    other.device_observation.seq = 6
    other.observation_id = "mock-device-01:mock-session-01:6"
    other.device_observation.field_summary.rms = 100.0
    args[2].append(other)
    assert "conflicting_observations" in verify_intent(*args).reason_codes
    other.device_observation.quality.status = "invalid"
    assert "sensor_quality_invalid" in verify_intent(*args).reason_codes


def test_unmapped_and_mutated_invalid_observation_fail_closed():
    args = setup()
    args[2][0].mapped_window_start_ms = None
    args[2][0].mapped_window_end_ms = None
    assert "time_mapping_missing" in verify_intent(*args).reason_codes
    args = setup()
    args[2][0].device_observation.field_summary.rms = float("nan")
    assert verify_intent(*args).reason_codes == ["invalid_input"]


def test_delayed_evidence_can_verify_original_action_within_mock_tolerance():
    adapter = build_mock_adapter("delayed")
    baseline = adapter.read_baseline("right")
    assert adapter.await_touch_result(run_id="run-1", action_id="action-1", zone_id="right", window_start_ms=500, window_end_ms=3500) == []
    late = adapter.await_touch_result(run_id="run-1", action_id="action-1", zone_id="right", window_start_ms=500, window_end_ms=3500, timeout_ms=1000)
    goal, _, _, context, calibration = setup()
    context.now_ms = 4500
    assert verify_intent(goal, baseline, late, context, calibration).status == "success"


@pytest.mark.parametrize("changes", [
    {"source": "live"}, {"increase_min": 1.0}, {"baseline_rms_max": 90.0},
    {"step_bands": [{"steps": 1, "delta_min": 0.0, "delta_max": 20.0}]},
    {"step_bands": [{"steps": 1, "delta_min": 18.0, "delta_max": 22.0}, {"steps": 2, "delta_min": 22.0, "delta_max": 40.0}]},
])
def test_invalid_or_real_source_profile_rejected(changes):
    payload = json.loads(PROFILE.read_text(encoding="utf-8"))
    payload.update(changes)
    with pytest.raises(ValidationError):
        MockCalibration.model_validate(payload)


def test_open_action_window_cannot_be_evaluated_as_complete():
    with pytest.raises(ValidationError):
        ActionContext(run_id="run-1", action_id="action-1", window_start_ms=500, window_end_ms=3500, now_ms=1500)


def test_mock_transport_conflicting_duplicate_cannot_produce_success():
    from app.adapters.mock import MockAdapter, ScheduledObservation
    goal, baseline, observations, context, calibration = setup()
    schedule = []
    for seq in range(5):
        d = baseline.device_observation.model_copy(deep=True)
        d.seq = seq
        d.window_start_ms, d.window_end_ms = seq * 100, (seq + 1) * 100
        schedule.append(ScheduledObservation((seq + 1) * 100, d))
    followup = observations[0].device_observation.model_copy(deep=True)
    conflicting = followup.model_copy(deep=True)
    conflicting.field_summary.rms = 140.0
    schedule.extend([ScheduledObservation(1500, followup), ScheduledObservation(1600, conflicting)])
    adapter = MockAdapter(schedule)
    baseline = adapter.read_baseline("right")
    after = adapter.await_touch_result(run_id=context.run_id, action_id=context.action_id, zone_id="right",
                                      window_start_ms=500, window_end_ms=3500)
    result = verify_intent(goal, baseline, after, context, calibration)
    assert result.status == "uncertain"
    assert result.goal_satisfied is None
    assert "sensor_quality_invalid" in result.reason_codes
