"""Bounded v0.2 input buffer shared by Serial and Replay adapters."""
from collections import deque
from datetime import datetime
from statistics import median
from app.contracts.models import DeviceObservation, Observation, Quality


class InputBuffer:
    def __init__(self, source: str, expected_device_id: str | None = None):
        if source not in ("live", "replay"):
            raise ValueError("source must be live or replay")
        self.source = source
        self.device_id = expected_device_id
        self.session_id: str | None = None
        self._retired: set[str] = set()
        self._last_seq = -1
        self._offsets: deque[int] = deque(maxlen=5)
        self._records: deque[Observation] = deque(maxlen=256)
        self._seen: dict[str, DeviceObservation] = {}
        self._returned: set[str] = set()
        self.errors: deque[str] = deque(maxlen=100)
        self.error: str | None = None

    def invalidate(self, reason: str) -> None:
        self.error = reason
        self.errors.append(reason)
        self._records.clear()
        self._offsets.clear()
        self._returned.clear()

    def reset(self) -> None:
        """Explicit reconnect/resynchronization; discard previous baseline/mapping."""
        self.invalidate("resynchronizing")
        self.error = None
        self.session_id = None
        self._retired.clear()
        self._last_seq = -1
        self._seen.clear()

    def ingest(self, device: DeviceObservation, now_ms: int, utc: datetime) -> None:
        device = DeviceObservation.model_validate_json(device.model_dump_json())
        if self.source == "live" and device.source != "live":
            self.invalidate("source_mismatch")
            return
        if self.device_id is None:
            self.device_id = device.device_id
        if device.device_id != self.device_id:
            self.invalidate("device_mismatch")
            return
        if device.session_id in self._retired:
            self.invalidate("retired_session")
            return
        if self.session_id is not None and self.session_id != device.session_id:
            self._retired.add(self.session_id)
            self.invalidate("session_changed")
            self._last_seq = -1
            self._seen.clear()
        self.session_id = device.session_id
        identifier = f"{device.device_id}:{device.session_id}:{device.seq}"
        previous = self._seen.get(identifier)
        if previous is not None:
            if previous != device:
                self.invalidate("conflicting_duplicate")
            return
        if device.seq <= self._last_seq:
            self.invalidate("sequence_regressed")
            return
        if self._last_seq >= 0 and device.seq != self._last_seq + 1:
            self._offsets.clear()
        self._last_seq = device.seq
        self._offsets.append(now_ms - device.window_end_ms)
        # Offset includes unknown transport delay. Never grant live verified status.
        offset = int(median(self._offsets)) if len(self._offsets) == 5 else None
        if offset is not None and device.window_start_ms + offset < 0:
            offset = None
        self._seen[identifier] = device.model_copy(deep=True)
        if len(self._seen) > 256:
            self._seen.pop(next(iter(self._seen)))
        if len(self._records) == 256:
            self._returned.discard(self._records[0].observation_id)
        device.source = self.source
        self._records.append(Observation(
            schema_version="0.2", observation_id=identifier, run_id=None, action_id=None,
            received_at=utc, received_monotonic_ms=now_ms,
            mapped_window_start_ms=None if offset is None else device.window_start_ms+offset,
            mapped_window_end_ms=None if offset is None else device.window_end_ms+offset,
            clock_quality="unverified", device_observation=device,
        ))

    def _copy(self, observation: Observation) -> Observation:
        result = observation.model_copy(deep=True)
        if self.error:
            result.device_observation.quality = Quality(status="invalid", reasons=[self.error])
        return result

    def read_baseline(self, zone_id: str) -> Observation | None:
        self.validate_zone(zone_id)
        for observation in reversed(self._records):
            if observation.device_observation.zone_id == zone_id:
                return self._copy(observation)
        return None

    def collect(self, run_id: str, action_id: str, zone_id: str, start_ms: int, end_ms: int) -> list[Observation]:
        self.validate_zone(zone_id)
        if not isinstance(run_id, str) or not run_id.strip() or not isinstance(action_id, str) or not action_id.strip():
            raise ValueError("run/action IDs are required")
        if type(start_ms) is not int or type(end_ms) is not int or start_ms < 0 or start_ms >= end_ms:
            raise ValueError("invalid action window")
        results = []
        for item in self._records:
            if item.observation_id in self._returned or item.device_observation.zone_id != zone_id:
                continue
            start, end = item.mapped_window_start_ms, item.mapped_window_end_ms
            # Missing mapping must be visible to the Verifier, not fabricated as no change.
            if start is not None and end is not None and (start >= end_ms or end <= start_ms):
                continue
            linked = self._copy(item)
            linked.run_id, linked.action_id = run_id, action_id
            results.append(linked)
            self._returned.add(item.observation_id)
        return results

    def check_sensor_integrity(self) -> Quality:
        if self.error:
            return Quality(status="invalid", reasons=[self.error])
        if not self._records:
            return Quality(status="invalid", reasons=["missing_samples"])
        quality = self._records[-1].device_observation.quality.model_copy(deep=True)
        if quality.status == "valid":
            quality.status = "degraded"
        if "clock_unverified" not in quality.reasons:
            quality.reasons.append("clock_unverified")
        return quality

    @staticmethod
    def validate_zone(zone_id: str) -> None:
        if zone_id not in ("right", "left"):
            raise ValueError("zone_id must be right or left")
