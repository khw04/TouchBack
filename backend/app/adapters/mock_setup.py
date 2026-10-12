"""Local development setup only. Do not register scenario selection as an Agent Tool."""
from typing import Literal

from app.adapters.mock import MockAdapter, ScheduledObservation, VirtualClock
from app.contracts.models import DeviceObservation, FieldSummary, Quality, Zone

MockScenario = Literal["normal", "no_change", "missing", "delayed", "duplicate", "sensor_error"]


def build_mock_adapter(scenario: MockScenario = "normal", *, zone_id: Zone = "right") -> MockAdapter:
    """Test signals, not real calibration or expected verdict labels."""
    if scenario not in ("normal", "no_change", "missing", "delayed", "duplicate", "sensor_error"):
        raise ValueError("unknown local mock scenario")
    if zone_id not in ("right", "left"):
        raise ValueError("zone_id must be right or left")

    def frame(seq: int, start: int, end: int, arrival: int, rms: float | None, beeps: int | None, quality: Quality):
        device = DeviceObservation(
            schema_version="0.2", device_id="mock-device-01", session_id="mock-session-01",
            seq=seq, source="mock", zone_id=zone_id, window_start_ms=start, window_end_ms=end,
            beep_count=beeps,
            field_summary=None if rms is None else FieldSummary(rms=rms, unit="adc_count", sample_count=end-start, sample_rate_hz=1000, method="ac_rms_v1"),
            quality=quality, calibration_id="mock-test-v1",
        )
        return ScheduledObservation(arrival, device)

    schedule = [frame(seq, seq*100, (seq+1)*100, (seq+1)*100, 100.0, 0, Quality(status="valid", reasons=[])) for seq in range(5)]
    if scenario != "missing":
        faulty = scenario == "sensor_error"
        followup = frame(5, 1000, 1500, 4500 if scenario == "delayed" else 1500,
                         None if faulty else (100.0 if scenario == "no_change" else 120.0),
                         None if faulty else (0 if scenario == "no_change" else 1),
                         Quality(status="invalid", reasons=["disconnected"]) if faulty else Quality(status="valid", reasons=[]))
        schedule.append(followup)
        if scenario == "duplicate":
            schedule.append(ScheduledObservation(1600, followup.observation.model_copy(deep=True)))
    return MockAdapter(schedule, VirtualClock(now_ms=500))
