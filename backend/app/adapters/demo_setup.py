"""Operator-only Mock inputs. Scenario names and labels never enter Agent messages."""
from typing import Literal
from app.adapters.mock import MockAdapter, ScheduledObservation, VirtualClock
from app.adapters.mock_setup import build_mock_adapter
from app.contracts.models import DeviceObservation, FieldSummary, Quality, Zone

DemoScenario = Literal["normal", "recovery", "sensor_error", "missing", "delayed", "no_change", "duplicate", "recovery_exhausted", "double_input"]
SCENARIOS = ("normal", "recovery", "sensor_error", "missing", "delayed", "no_change", "duplicate", "recovery_exhausted", "double_input")


def build_demo_adapter(scenario: DemoScenario, *, zone_id: Zone = "right") -> MockAdapter:
    if scenario not in SCENARIOS or zone_id not in ("right", "left"):
        raise ValueError("unsupported demo scenario or zone")
    if scenario not in ("recovery", "recovery_exhausted"):
        return build_mock_adapter(scenario, zone_id=zone_id)
    def frame(seq,start,end,arrival,rms):
        device = DeviceObservation(schema_version="0.2",device_id="mock-device-01",session_id="mock-session-01",
            seq=seq,source="mock",zone_id=zone_id,window_start_ms=start,window_end_ms=end,
            beep_count=1 if rms>105 else 0,
            field_summary=FieldSummary(rms=rms,unit="adc_count",sample_count=end-start,sample_rate_hz=1000,method="ac_rms_v1"),
            quality=Quality(status="valid",reasons=[]),calibration_id="mock-test-v1")
        return ScheduledObservation(arrival,device)
    schedule=[frame(seq,seq*100,(seq+1)*100,(seq+1)*100,100.0) for seq in range(5)]
    schedule += [frame(5,1000,1500,1500,100.0),frame(6,4000,4500,4500,100.0),
                 frame(7,5000,5500,5500,120.0 if scenario=="recovery" else 100.0)]
    return MockAdapter(schedule,VirtualClock(now_ms=500))
