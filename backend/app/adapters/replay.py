"""Replay host Observation JSON Lines with original timing/source kept outside Agent input."""
from pathlib import Path
from app.adapters.input_buffer import InputBuffer
from app.adapters.mock import VirtualClock
from app.adapters.serial_input import unique_object
from app.contracts.models import Observation
import json


class ReplayAdapter:
    def __init__(self, records: list[Observation]):
        self._records = [Observation.model_validate_json(item.model_dump_json()) for item in records]
        if any(b.received_monotonic_ms < a.received_monotonic_ms for a, b in zip(self._records, self._records[1:])):
            raise ValueError("record receive times must be ordered")
        self.recorded_sources = tuple(item.device_observation.source for item in self._records)
        self.clock = VirtualClock(self._records[0].received_monotonic_ms if self._records else 0)
        self.buffer = InputBuffer("replay")
        self._index = 0

    @classmethod
    def from_file(cls, path: str | Path):
        records = []
        with Path(path).open("rb") as stream:
            while line := stream.readline(8193):
                if len(line) > 8192 or not line.endswith(b"\n"):
                    raise ValueError("invalid replay line length or missing LF")
                records.append(Observation.model_validate_json(json.dumps(json.loads(line.decode("utf-8"), object_pairs_hook=unique_object))))
        return cls(records)

    def _receive(self):
        while self._index < len(self._records) and self._records[self._index].received_monotonic_ms <= self.clock.now_ms:
            record = self._records[self._index]
            self.buffer.ingest(record.device_observation, record.received_monotonic_ms, record.received_at)
            self._index += 1

    def read_baseline(self, zone_id):
        self.buffer.validate_zone(zone_id)
        self._receive()
        return self.buffer.read_baseline(zone_id)

    def await_touch_result(self, *, run_id, action_id, zone_id, window_start_ms, window_end_ms, timeout_ms=3000):
        if type(timeout_ms) is not int or timeout_ms < 0:
            raise ValueError("timeout_ms must be a nonnegative integer")
        self.buffer.validate_zone(zone_id)
        if not isinstance(run_id, str) or not run_id.strip() or not isinstance(action_id, str) or not action_id.strip() or type(window_start_ms) is not int or type(window_end_ms) is not int or not 0 <= window_start_ms < window_end_ms:
            raise ValueError("invalid action arguments")
        self.clock.advance_to(self.clock.now_ms + timeout_ms)
        self._receive()
        return self.buffer.collect(run_id, action_id, zone_id, window_start_ms, window_end_ms)

    def check_sensor_integrity(self):
        self._receive()
        return self.buffer.check_sensor_integrity()
