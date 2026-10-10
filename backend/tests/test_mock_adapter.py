import pytest

from app.adapters.mock import MockAdapter, ScheduledObservation, VirtualClock
from app.adapters.mock_setup import build_mock_adapter
from app.contracts.models import DeviceObservation, Observation


def collect(adapter, **changes):
    params = dict(run_id="run-1", action_id="action-1", zone_id="right", window_start_ms=500, window_end_ms=3500, timeout_ms=3000)
    params.update(changes)
    return adapter.await_touch_result(**params)


def test_normal_baseline_and_followup_are_sensor_evidence_only():
    adapter = build_mock_adapter()
    baseline = adapter.read_baseline("right")
    assert baseline.device_observation.field_summary.rms == 100.0
    assert baseline.run_id is None and baseline.action_id is None
    assert baseline.clock_quality == "verified"
    assert adapter.check_sensor_integrity().status == "valid"
    observations = collect(adapter)
    assert len(observations) == 1
    result = observations[0]
    assert result.device_observation.field_summary.rms == 120.0
    assert result.device_observation.beep_count == 1
    assert (result.run_id, result.action_id) == ("run-1", "action-1")
    assert Observation.model_validate_json(result.model_dump_json()) == result
    payload = result.model_dump_json()
    for label in ("scenario", "normal", "expected", "verdict", "success"):
        assert label not in payload
    assert result.device_observation.source == "mock"


def test_no_change_preserves_measured_zero_beeps():
    adapter = build_mock_adapter("no_change")
    baseline = adapter.read_baseline("right")
    result = collect(adapter)[0]
    assert result.device_observation.field_summary.rms == baseline.device_observation.field_summary.rms
    assert result.device_observation.beep_count == 0
    assert result.device_observation.quality.status == "valid"


def test_missing_returns_empty_not_fabricated_zero():
    adapter = build_mock_adapter("missing")
    assert collect(adapter) == []
    assert collect(adapter) == []


def test_delayed_result_retains_original_window_on_later_check():
    adapter = build_mock_adapter("delayed")
    assert collect(adapter) == []
    assert adapter.clock.now_ms == 3500
    result = collect(adapter, timeout_ms=1000)[0]
    assert result.received_monotonic_ms == 4500
    assert (result.mapped_window_start_ms, result.mapped_window_end_ms) == (1000, 1500)
    assert result.action_id == "action-1"
    assert collect(adapter, timeout_ms=0) == []


def test_duplicate_is_not_returned_twice_or_linked_to_another_action():
    adapter = build_mock_adapter("duplicate")
    assert len(collect(adapter)) == 1
    assert collect(adapter, action_id="action-2", timeout_ms=0) == []


def test_sensor_error_remains_invalid_and_unknown():
    adapter = build_mock_adapter("sensor_error")
    result = collect(adapter)[0]
    assert result.device_observation.field_summary is None
    assert result.device_observation.beep_count is None
    assert result.device_observation.quality.status == "invalid"
    assert adapter.check_sensor_integrity().reasons == ["disconnected"]


def test_clock_mapping_is_not_verified_before_five_unique_samples():
    adapter = build_mock_adapter()
    adapter.clock = VirtualClock(100)
    baseline = adapter.read_baseline("right")
    assert baseline.clock_quality == "unverified"
    assert adapter.check_sensor_integrity().status == "degraded"
    adapter.clock.advance_to(500)
    assert adapter.read_baseline("right").clock_quality == "verified"


def test_wrong_zone_is_not_returned_and_does_not_consume_evidence():
    adapter = build_mock_adapter(zone_id="left")
    assert adapter.read_baseline("right") is None
    assert collect(adapter) == []
    assert len(collect(adapter, zone_id="left", timeout_ms=0)) == 1


def test_boundary_overlap_is_kept_for_verifier_not_promoted_or_discarded():
    adapter = build_mock_adapter()
    result = collect(adapter, window_start_ms=1250)[0]
    assert result.mapped_window_start_ms == 1000


def test_outside_window_stays_buffered_for_correct_query():
    adapter = build_mock_adapter()
    assert collect(adapter, window_start_ms=1500, window_end_ms=3500) == []
    assert len(collect(adapter, timeout_ms=0)) == 1


def test_returned_mutations_do_not_modify_baseline_or_buffer():
    adapter = build_mock_adapter()
    first = adapter.read_baseline("right")
    first.device_observation.field_summary.rms = 999.0
    assert adapter.read_baseline("right").device_observation.field_summary.rms == 100.0
    result = collect(adapter)[0]
    result.device_observation.quality.reasons.append("injected")
    assert adapter.read_baseline("right").device_observation.quality.reasons == []


def test_new_instances_reproduce_identical_evidence():
    first, second = build_mock_adapter(), build_mock_adapter()
    assert collect(first) == collect(second)


@pytest.mark.parametrize("changes", [{"timeout_ms": -1}, {"timeout_ms": True}, {"window_start_ms": -1}, {"window_end_ms": 500}, {"run_id": " "}, {"action_id": ""}, {"zone_id": "unknown"}])
def test_invalid_wait_arguments_do_not_advance_clock(changes):
    adapter = build_mock_adapter()
    with pytest.raises(ValueError):
        collect(adapter, **changes)
    assert adapter.clock.now_ms == 500


def test_empty_adapter_has_explicit_integrity_error():
    adapter = MockAdapter([])
    assert adapter.read_baseline("right") is None
    assert adapter.check_sensor_integrity().reasons == ["missing_samples"]


def test_virtual_clock_refuses_backwards_time():
    clock = VirtualClock()
    with pytest.raises(ValueError):
        clock.advance_to(499)


def device(seq=0, session="boot-1", source="mock"):
    return DeviceObservation(schema_version="0.2", device_id="device-1", session_id=session,
        seq=seq, source=source, zone_id="right", window_start_ms=1000, window_end_ms=1500,
        beep_count=None, field_summary=None, quality={"status": "valid", "reasons": []}, calibration_id=None)


@pytest.mark.parametrize("kind", ["sequence", "session"])
def test_transport_regression_is_visible_in_integrity_and_evidence(kind):
    schedule = [ScheduledObservation(1500, device(seq=2)), ScheduledObservation(1600, device(seq=1, session="boot-2" if kind == "session" else "boot-1"))]
    adapter = MockAdapter(schedule)
    result = collect(adapter)[0]
    assert result.device_observation.quality.status == "invalid"
    assert adapter.check_sensor_integrity().status == "invalid"


def test_mock_refuses_live_source_or_arrival_before_measurement_end():
    with pytest.raises(ValueError):
        ScheduledObservation(1500, device(source="live"))
    with pytest.raises(ValueError):
        ScheduledObservation(1400, device())


def test_schedule_input_is_copied():
    original = device()
    adapter = MockAdapter([ScheduledObservation(1500, original)])
    original.beep_count = 999
    assert collect(adapter)[0].device_observation.beep_count is None


def test_sequence_gap_resets_clock_confirmation_samples():
    schedule = [ScheduledObservation(1500+i*100, device(seq=seq)) for i, seq in enumerate([0, 1, 2, 3, 9])]
    adapter = MockAdapter(schedule)
    collect(adapter)
    assert adapter.read_baseline("right").clock_quality == "unverified"
    assert "clock_unverified" in adapter.check_sensor_integrity().reasons
