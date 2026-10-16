"""Synthetic transports only; no physical acquisition or calibration claims."""
import json
from pathlib import Path

import pytest

from app.adapters.replay import ReplayAdapter
from app.adapters.serial_input import SerialAdapter
from app.contracts.models import DeviceObservation
from app.recording.capture import capture, main

EXAMPLE = Path(__file__).resolve().parents[2] / "docs/examples/device-observation.json"


def frame(seq=0, session="boot-1", source="live"):
    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    data.update(seq=seq, session_id=session, source=source, window_start_ms=seq*100,
                window_end_ms=(seq+1)*100)
    return DeviceObservation.model_validate(data)


class Transport:
    timeout = .05

    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.closed = False

    def read(self, count):
        item = next(self.chunks, b"")
        if isinstance(item, BaseException):
            raise item
        return item

    def write(self, data):
        raise AssertionError("capture must never send physical commands")

    def close(self):
        self.closed = True


def run_capture(tmp_path, chunks):
    transport = Transport(chunks)
    tick = [0.0]
    def clock():
        tick[0] += .001
        return tick[0]
    def factory(port, **options):
        return SerialAdapter(transport, now_ms=lambda: 10000, **{k: v for k, v in options.items() if k != "baudrate"})
    path = tmp_path / "capture"
    summary = capture("TEST", path, duration_seconds=.02, adapter_factory=factory, monotonic=clock)
    assert transport.closed
    assert summary == json.loads((path / "summary.json").read_text(encoding="utf-8"))
    return path, summary


def test_capture_every_frame_even_when_input_buffer_rolls_over(tmp_path):
    chunks = [(frame(i).model_dump_json()+"\n").encode() for i in range(300)]
    # One read contains >256 complete frames; recorder must not sample just the latest baseline.
    path, summary = run_capture(tmp_path, [b"".join(chunks)])
    assert summary["observations"] == 300
    assert summary["error_count"] == 0 and summary["status"] == "completed"
    replay = ReplayAdapter.from_file(path / "observations.jsonl")
    assert len(replay.recorded_sources) == 300
    assert set(replay.recorded_sources) == {"live"}
    assert summary["clock_quality"] == "unverified"
    assert summary["physical_validation_performed"] is False


def test_fragmented_duplicate_corrupt_and_conflicting_frames_are_explicit(tmp_path):
    data = (frame().model_dump_json()+"\n").encode()
    conflicting = frame(); conflicting.beep_count = 77
    path, summary = run_capture(tmp_path, [data[:13],data[13:]+data+b"not-json\n"+
                                          (conflicting.model_dump_json()+"\n").encode()])
    assert summary["observations"] == 1
    assert summary["errors"]["parse_error"] == 1
    assert summary["errors"]["conflicting_duplicate"] == 1
    assert len((path / "events.jsonl").read_text().splitlines()) == 2


def test_reboot_and_source_mismatch_are_not_silently_ignored(tmp_path):
    payload = b"".join((x.model_dump_json()+"\n").encode() for x in [frame(),frame(0,"boot-2"),frame(1,"boot-2","mock")])
    path, summary = run_capture(tmp_path,[payload])
    assert summary["observations"] == 2
    assert summary["errors"] == {"session_changed":1,"source_mismatch":1}
    records = (path / "observations.jsonl").read_text().splitlines()
    assert json.loads(records[-1])["device_observation"]["quality"]["status"] == "invalid"


@pytest.mark.parametrize("failure,status,reason", [(OSError(),"disconnected","disconnected"),
                                                   (KeyboardInterrupt(),"interrupted","no_observations")])
def test_disconnect_or_interrupt_closes_and_preserves_summary(tmp_path,failure,status,reason):
    _, summary = run_capture(tmp_path,[failure])
    assert summary["status"] == status
    assert summary["errors"][reason] >= 1


def test_partial_frame_at_stop_is_recorded(tmp_path):
    _, summary = run_capture(tmp_path,[b"partial"])
    assert summary["errors"] == {"incomplete_line":1,"no_observations":1}


def test_failed_open_records_failure_without_private_exception_text(tmp_path):
    def fail(*args,**kwargs):
        raise OSError("private port diagnostic")
    path = tmp_path / "capture"
    result = capture("TEST",path,adapter_factory=fail)
    assert result["status"] == "failed"
    assert result["errors"]["capture_failed"] == 1
    assert "private port diagnostic" not in (path / "events.jsonl").read_text()


@pytest.mark.parametrize("duration", [0,-1,float("nan"),float("inf"),3601,True])
def test_invalid_duration_creates_no_output(tmp_path,duration):
    path = tmp_path / "capture"
    with pytest.raises(ValueError):
        capture("TEST",path,duration_seconds=duration)
    assert not path.exists()


def test_existing_capture_is_not_overwritten_or_port_opened(tmp_path):
    path = tmp_path / "capture"; path.mkdir()
    sentinel = path / "observations.jsonl"; sentinel.write_text("existing")
    with pytest.raises(FileExistsError):
        capture("TEST",path,adapter_factory=lambda *a,**k: pytest.fail("port opened"))
    assert sentinel.read_text() == "existing"


def test_cli_returns_nonzero_for_failed_capture(tmp_path,monkeypatch):
    monkeypatch.setattr("app.recording.capture.capture", lambda *a,**k: {"status":"failed","error_count":1})
    assert main(["--port","TEST","--output-dir",str(tmp_path / "new")]) == 1

def test_recording_callback_io_error_is_not_mislabelled_as_serial_disconnect():
    data = (frame().model_dump_json()+"\n").encode()
    transport = Transport([data])
    errors = []
    def fail(record):
        raise OSError("synthetic storage failure")
    adapter = SerialAdapter(transport,on_observation=fail,on_error=errors.append)
    try:
        with pytest.raises(OSError,match="synthetic storage"):
            adapter.poll()
        assert "disconnected" not in errors
    finally:
        adapter.close()

def test_skipped_sequence_is_visible_in_capture_summary(tmp_path):
    data=b"".join((x.model_dump_json()+"\n").encode() for x in [frame(0),frame(2)])
    _, summary=run_capture(tmp_path,[data])
    assert summary["observations"] == 2
    assert summary["errors"] == {"sequence_gap":1}
