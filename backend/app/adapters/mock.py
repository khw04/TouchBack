"""Deterministic mock transport with controlled time, never real sensor evidence."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.contracts.models import DeviceObservation, Observation, Quality, Zone


def nonnegative(value: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


@dataclass
class VirtualClock:
    now_ms: int = 500

    def __post_init__(self) -> None:
        nonnegative(self.now_ms, "now_ms")

    def advance_to(self, target_ms: int) -> None:
        nonnegative(target_ms, "target_ms")
        if target_ms < self.now_ms:
            raise ValueError("virtual clock cannot move backwards")
        self.now_ms = target_ms


@dataclass(frozen=True)
class ScheduledObservation:
    arrival_ms: int
    observation: DeviceObservation

    def __post_init__(self) -> None:
        nonnegative(self.arrival_ms, "arrival_ms")
        if self.arrival_ms < self.observation.window_end_ms:
            raise ValueError("mock arrival cannot precede measurement end")
        if self.observation.source != "mock":
            raise ValueError("mock adapter accepts only mock observations")


class MockAdapter:
    """Single mock device/session. Times share a controlled virtual clock.

    Waits advance virtual time without wall-clock sleeping. Caller supplies the
    action window; execution-state/ack/cancellation policy belongs to the Agent.
    """

    def __init__(self, schedule: list[ScheduledObservation], clock: VirtualClock | None = None):
        self.clock = clock if clock is not None else VirtualClock()
        self._pending = sorted(
            [ScheduledObservation(frame.arrival_ms, DeviceObservation.model_validate_json(frame.observation.model_dump_json())) for frame in schedule],
            key=lambda frame: frame.arrival_ms,
        )
        self._buffer: list[Observation] = []
        self._seen: set[str] = set()
        self._returned: set[str] = set()
        self._session: tuple[str, str] | None = None
        self._last_seq = -1
        self._clock_samples = 0
        self._transport_error: str | None = None

    def _receive(self) -> None:
        while self._pending and self._pending[0].arrival_ms <= self.clock.now_ms:
            frame = self._pending.pop(0)
            # Validate again and copy so fixture mutation cannot change received evidence.
            device = DeviceObservation.model_validate_json(frame.observation.model_dump_json())
            session = (device.device_id, device.session_id)
            if self._session is None:
                self._session = session
            if session != self._session:
                self._transport_error = "session_changed"
                continue  # reboot/multiple-device simulation belongs to the live adapter ticket
            identifier = f"{device.device_id}:{device.session_id}:{device.seq}"
            if identifier in self._seen:
                continue
            if device.seq <= self._last_seq:
                self._transport_error = "sequence_regressed"
                continue
            if self._last_seq >= 0 and device.seq != self._last_seq + 1:
                self._clock_samples = 0
            self._seen.add(identifier)
            self._last_seq = device.seq
            self._clock_samples += 1
            verified = self._clock_samples >= 5
            self._buffer.append(Observation(
                schema_version="0.2", observation_id=identifier, run_id=None, action_id=None,
                received_at=datetime(2026, 10, 10, tzinfo=timezone.utc) + timedelta(milliseconds=frame.arrival_ms),
                received_monotonic_ms=frame.arrival_ms,
                mapped_window_start_ms=device.window_start_ms,
                mapped_window_end_ms=device.window_end_ms,
                clock_quality="verified" if verified else "unverified",
                device_observation=device,
            ))

    def read_baseline(self, zone_id: Zone) -> Observation | None:
        """Latest arrived observation; quality/age/calibration are not verdicts."""
        self._validate_zone(zone_id)
        self._receive()
        for observation in reversed(self._buffer):
            if observation.device_observation.zone_id == zone_id:
                return self._copy_evidence(observation)
        return None

    def await_touch_result(
        self, *, run_id: str, action_id: str, zone_id: Zone,
        window_start_ms: int, window_end_ms: int, timeout_ms: int = 3000,
    ) -> list[Observation]:
        """Return arrived evidence overlapping the action window, once per adapter.

        Keep boundary-overlapping/invalid evidence visible for the Verifier to
        reject; arrival time never replaces the measurement window. A later call
        can collect a delayed frame for the original action before re-guidance.
        """
        self._validate_zone(zone_id)
        if not isinstance(run_id, str) or not run_id.strip() or not isinstance(action_id, str) or not action_id.strip():
            raise ValueError("run_id and action_id must be nonempty strings")
        for name, value in (("window_start_ms", window_start_ms), ("window_end_ms", window_end_ms), ("timeout_ms", timeout_ms)):
            nonnegative(value, name)
        if window_start_ms >= window_end_ms:
            raise ValueError("action window must satisfy start < end")
        self.clock.advance_to(self.clock.now_ms + timeout_ms)
        self._receive()
        results = []
        for observation in self._buffer:
            if observation.observation_id in self._returned:
                continue
            if observation.device_observation.zone_id != zone_id:
                continue
            start, end = observation.mapped_window_start_ms, observation.mapped_window_end_ms
            if start is None or end is None or start >= window_end_ms or end <= window_start_ms:
                continue
            linked = self._copy_evidence(observation)
            linked.run_id, linked.action_id = run_id, action_id
            self._returned.add(observation.observation_id)
            results.append(linked)
        return results

    def check_sensor_integrity(self) -> Quality:
        self._receive()
        if self._transport_error:
            return Quality(status="invalid", reasons=[self._transport_error])
        if not self._buffer:
            return Quality(status="invalid", reasons=["missing_samples"])
        latest = self._buffer[-1]
        quality = latest.device_observation.quality.model_copy(deep=True)
        if latest.clock_quality == "unverified":
            if quality.status == "valid":
                quality.status = "degraded"
            if "clock_unverified" not in quality.reasons:
                quality.reasons.append("clock_unverified")
        return quality

    def _copy_evidence(self, observation: Observation) -> Observation:
        copied = observation.model_copy(deep=True)
        if self._transport_error:
            copied.device_observation.quality = Quality(status="invalid", reasons=[self._transport_error])
        return copied

    @staticmethod
    def _validate_zone(zone_id: Zone) -> None:
        if zone_id not in ("right", "left"):
            raise ValueError("zone_id must be right or left")
