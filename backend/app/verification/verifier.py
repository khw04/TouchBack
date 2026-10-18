"""Entry point for skills/intent-output-verification/SKILL.md."""
from pydantic import ValidationError
from app.contracts.models import Goal, Observation, Verdict
from app.verification.models import ActionContext, MockCalibration


def verdict(status: str, level: str, satisfied: bool | None, reasons: list[str], evidence: list[str]) -> Verdict:
    return Verdict(status=status, verification_level=level, goal_satisfied=satisfied,
                   reason_codes=list(dict.fromkeys(reasons)), evidence_ids=list(dict.fromkeys(evidence)))


def observation_calibration_errors(
    item: Observation, calibration: MockCalibration | None, zone_id: str, *, baseline: bool = False,
) -> list[str]:
    """Shared measurement/profile checks for preflight and final verification."""
    if calibration is None:
        return ["calibration_missing"]
    try:
        item = Observation.model_validate_json(item.model_dump_json())
        calibration = MockCalibration.model_validate_json(calibration.model_dump_json())
    except (ValueError, TypeError, AttributeError):
        return ["invalid_input"]
    reasons = []
    device = item.device_observation
    field = device.field_summary
    if calibration.zone_id != zone_id:
        reasons.append("calibration_scope_mismatch")
    if device.source != calibration.source:
        reasons.append("source_mismatch")
    if (device.device_id, device.session_id) != (calibration.device_id, calibration.session_id):
        reasons.append("device_session_mismatch")
    if device.zone_id != zone_id:
        reasons.append("zone_mismatch")
    if device.calibration_id != calibration.calibration_id:
        reasons.append("calibration_mismatch")
    start, end = item.mapped_window_start_ms, item.mapped_window_end_ms
    if field is None:
        reasons.append("field_missing")
    else:
        if start is not None and end is not None and field.sample_count < (end - start) * field.sample_rate_hz / 1000:
            reasons.append("insufficient_samples")
        if (field.unit, field.method, field.sample_rate_hz) != (calibration.unit, calibration.method, calibration.sample_rate_hz):
            reasons.append("measurement_settings_mismatch")
        if baseline and not calibration.baseline_rms_min <= field.rms <= calibration.baseline_rms_max:
            reasons.append("baseline_outside_calibration")
    return reasons


def verify_intent(
    goal: Goal, baseline: Observation | None, observations: list[Observation],
    context: ActionContext, calibration: MockCalibration | None,
) -> Verdict:
    """Fail closed on any relevant invalid evidence; never infer a verdict from beeps."""
    reasons: list[str] = []
    evidence: list[str] = []
    try:
        goal = Goal.model_validate_json(goal.model_dump_json())
        context = ActionContext.model_validate_json(context.model_dump_json())
        if calibration is not None:
            calibration = MockCalibration.model_validate_json(calibration.model_dump_json())
        baseline = None if baseline is None else Observation.model_validate_json(baseline.model_dump_json())
        observations = [Observation.model_validate_json(item.model_dump_json()) for item in observations]
    except (ValidationError, ValueError):
        return verdict("uncertain", "none", None, ["invalid_input"], [])
    if baseline is None:
        reasons.append("baseline_missing")
    if not observations:
        reasons.append("observations_missing")
    if calibration is None:
        reasons.append("calibration_missing")
    if reasons:
        return verdict("uncertain", "none", None, reasons, [])

    assert baseline is not None and calibration is not None
    if calibration.zone_id != goal.zone_id:
        reasons.append("calibration_scope_mismatch")
    unique: list[Observation] = []
    seen: dict[str, Observation] = {}
    for item in observations:
        previous = seen.get(item.observation_id)
        if previous is not None:
            if previous != item:
                reasons.append("conflicting_duplicate")
            continue
        seen[item.observation_id] = item
        unique.append(item)

    for item in [baseline, *unique]:
        evidence.append(item.observation_id)
        d = item.device_observation
        reasons.extend(observation_calibration_errors(item, calibration, goal.zone_id, baseline=item is baseline))
        if d.quality.status != "valid" or d.quality.reasons:
            reasons.append("sensor_quality_invalid")
        if item.clock_quality != "verified":
            reasons.append("clock_unverified")
        start, end = item.mapped_window_start_ms, item.mapped_window_end_ms
        if start is None or end is None:
            reasons.append("time_mapping_missing")
        else:
            if item.received_monotonic_ms < end or item.received_monotonic_ms > context.now_ms:
                reasons.append("invalid_received_time")
            if item.received_monotonic_ms - end > calibration.max_transport_delay_ms:
                reasons.append("transport_delay_exceeded")
            if item is baseline:
                if end > context.window_start_ms or item.received_monotonic_ms > context.window_start_ms:
                    reasons.append("baseline_after_instruction")
                if context.window_start_ms - end > calibration.max_baseline_age_ms:
                    reasons.append("baseline_stale")
            else:
                if start < context.window_start_ms or end > context.window_end_ms:
                    reasons.append("outside_action_window")
                if context.now_ms - end > calibration.max_observation_age_ms:
                    reasons.append("observation_stale")
                if end - start < calibration.min_observation_ms:
                    reasons.append("observation_window_too_short")

    base = baseline.device_observation
    last_seq = base.seq
    for item in unique:
        if (item.run_id, item.action_id) != (context.run_id, context.action_id):
            reasons.append("action_mismatch")
        if item.device_observation.seq <= last_seq:
            reasons.append("sequence_regressed")
        last_seq = item.device_observation.seq
    if reasons:
        return verdict("uncertain", "none", None, reasons, evidence)

    assert base.field_summary is not None
    outcomes: list[tuple[str, str, bool | None, str]] = []
    for item in unique:
        field = item.device_observation.field_summary
        assert field is not None
        delta = field.rms - base.field_summary.rms
        band = next((band for band in calibration.step_bands if band.delta_min <= delta <= band.delta_max), None)
        if band is not None:
            satisfied = band.steps == goal.requested_steps
            outcomes.append(("success" if satisfied else "mismatch", "exact_step", satisfied,
                             "requested_step_verified" if satisfied else "different_step_verified"))
        elif abs(delta) <= calibration.no_change_tolerance:
            outcomes.append(("mismatch", "none", False, "no_output_change"))
        elif delta >= calibration.increase_min:
            outcomes.append(("uncertain", "output_change", None, "step_unverified"))
        else:
            outcomes.append(("uncertain", "none", None, "change_unclassified"))
    if len(set(outcomes)) != 1:
        return verdict("uncertain", "none", None, ["conflicting_observations"], evidence)
    status, level, satisfied, reason = outcomes[0]
    return verdict(status, level, satisfied, [reason], evidence)
