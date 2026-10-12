"""Read-only Serial JSON Lines ingestion; no commands are sent to the induction."""
import json
import time
from datetime import datetime, timezone
from typing import Callable
from pydantic import ValidationError
from app.adapters.input_buffer import InputBuffer
from app.contracts.models import DeviceObservation


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


class JsonLinesDecoder:
    def __init__(self, on_observation: Callable, on_error: Callable):
        self._line = bytearray()
        self._discard = False
        self.on_observation = on_observation
        self.on_error = on_error

    def feed(self, chunk: bytes) -> None:
        for byte in chunk:
            if byte == 10:
                if not self._discard:
                    try:
                        payload = json.loads(self._line.decode("utf-8"), object_pairs_hook=unique_object)
                        device = DeviceObservation.model_validate(payload)
                    except (ValueError, UnicodeError, ValidationError):
                        self.on_error("parse_error")
                    else:
                        self.on_observation(device)
                self._line.clear()
                self._discard = False
            elif not self._discard:
                self._line.append(byte)
                if len(self._line) >= 8192:  # payload + LF must be <= 8192 bytes
                    self._line.clear()
                    self._discard = True
                    self.on_error("line_too_long")

    def finish(self) -> None:
        if self._line or self._discard:
            self.on_error("incomplete_line")
        self._line.clear()
        self._discard = False


class SerialAdapter:
    def __init__(self, transport, *, expected_device_id=None, now_ms=None, utc_now=None):
        timeout = getattr(transport, "timeout", None)
        if timeout is None or not 0 <= timeout <= .1:
            raise ValueError("transport read timeout must be between 0 and 0.1 seconds")
        self._transport = transport
        self._now_ms = now_ms or (lambda: time.monotonic_ns() // 1_000_000)
        self._utc_now = utc_now or (lambda: datetime.now(timezone.utc))
        self.buffer = InputBuffer("live", expected_device_id)
        self._closed = False
        self._decoder = JsonLinesDecoder(self._ingest, self.buffer.invalidate)

    @classmethod
    def open(cls, port: str, *, baudrate: int = 115200, expected_device_id=None):
        import serial
        transport = serial.Serial(port=port, baudrate=baudrate, timeout=.05, write_timeout=.05)
        return cls(transport, expected_device_id=expected_device_id)

    def _ingest(self, observation):
        self.buffer.ingest(observation, self._now_ms(), self._utc_now())

    def poll(self) -> None:
        if self._closed:
            return
        try:
            self._decoder.feed(self._transport.read(4096))
        except OSError:
            self.buffer.invalidate("disconnected")
            self.close()

    def read_baseline(self, zone_id):
        self.buffer.validate_zone(zone_id)
        self.poll()
        return self.buffer.read_baseline(zone_id)

    def await_touch_result(self, *, run_id, action_id, zone_id, window_start_ms, window_end_ms, timeout_ms=3000, cancelled=None):
        if type(timeout_ms) is not int or timeout_ms < 0:
            raise ValueError("timeout_ms must be a nonnegative integer")
        # Validate before polling/consuming any evidence.
        self.buffer.validate_zone(zone_id)
        if not isinstance(run_id, str) or not run_id.strip() or not isinstance(action_id, str) or not action_id.strip() or type(window_start_ms) is not int or type(window_end_ms) is not int or not 0 <= window_start_ms < window_end_ms:
            raise ValueError("invalid action arguments")
        deadline = self._now_ms() + timeout_ms
        while not self._closed and self._now_ms() < deadline:
            if cancelled is not None and cancelled():
                self.buffer.invalidate("cancelled")
                return []
            self.poll()
            time.sleep(.001)
        if cancelled is not None and cancelled():
            self.buffer.invalidate("cancelled")
            return []
        return self.buffer.collect(run_id, action_id, zone_id, window_start_ms, window_end_ms)

    def check_sensor_integrity(self):
        return self.buffer.check_sensor_integrity()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._decoder.finish()
            self._transport.close()
            self.buffer.invalidate("disconnected")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
