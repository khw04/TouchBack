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

def test_close_finally_releases_transport_when_partial_error_recording_fails():
    transport=Transport([b"partial"])
    def error(reason):
        if reason == "incomplete_line":
            raise OSError("synthetic diagnostic write failure")
    adapter=SerialAdapter(transport,on_error=error)
    adapter.poll()
    with pytest.raises(OSError,match="diagnostic write failure"):
        adapter.close()
    assert transport.closed and adapter.closed
    adapter.close()  # Repeated close remains harmless.


def test_capture_summary_failed_but_port_closed_when_shutdown_event_write_fails(tmp_path,monkeypatch):
    original=Path.open
    transports=[]
    class EventWriter:
        failed=False
        def __init__(self,stream):self.stream=stream
        def __enter__(self):return self
        def __exit__(self,*args):return self.stream.__exit__(*args)
        def write(self,text):
            if 'incomplete_line' in text and not self.failed:
                self.failed=True
                raise OSError("synthetic shutdown write failure")
            return self.stream.write(text)
        def flush(self):self.stream.flush()
    def opened(path,*args,**kwargs):
        stream=original(path,*args,**kwargs)
        return EventWriter(stream) if path.name == "events.jsonl" and args and args[0] == "x" else stream
    monkeypatch.setattr(Path,"open",opened)
    def factory(port,**options):
        transport=Transport([(frame().model_dump_json()+"\npartial").encode()])
        transports.append(transport)
        return SerialAdapter(transport,**{k:v for k,v in options.items() if k!='baudrate'})
    tick=[0.0]
    def clock():tick[0]+=.001;return tick[0]
    result=capture("TEST",tmp_path/"capture",duration_seconds=.02,adapter_factory=factory,monotonic=clock)
    assert result["status"] == "failed" and result["observations"] == 1
    assert result["errors"]["close_failed"] == 1
    assert transports[0].closed

# Even within the existing 8192-byte limit, native JSON recursion can fail.
DEEP_JSON = [b'[' * 3500 + b'0' + b']' * 3500,
             b'[' * 4000 + b'0' + b']' * 4000]


@pytest.mark.parametrize('payload', DEEP_JSON, ids=['depth-3500','depth-4000'])
def test_deep_json_is_parse_error_and_next_frame_survives(payload):
    from app.adapters.serial_input import JsonLinesDecoder
    observations, errors = [], []
    decoder = JsonLinesDecoder(observations.append, errors.append)
    data = payload + b'\n' + (frame().model_dump_json() + '\n').encode()
    assert len(payload) + 1 <= 8192
    decoder.feed(data[:100])
    decoder.feed(data[100:])
    decoder.finish()
    assert errors == ['parse_error']
    assert observations == [frame()]


@pytest.mark.parametrize('payload', DEEP_JSON, ids=['depth-3500','depth-4000'])
def test_replay_rejects_deep_json_as_value_error_without_partial_loading(tmp_path, payload):
    path = tmp_path / 'deep.jsonl'
    path.write_bytes(payload + b'\n')
    with pytest.raises(ValueError, match='invalid replay observation JSON'):
        ReplayAdapter.from_file(path)


def test_capture_keeps_following_frame_after_deep_json_and_records_error(tmp_path):
    payload = DEEP_JSON[0] + b'\n' + (frame().model_dump_json() + '\n').encode()
    path, summary = run_capture(tmp_path, [payload[:100], payload[100:]])
    assert summary['observations'] == 1
    assert summary['errors'] == {'parse_error':1}
    records = (path/'observations.jsonl').read_text(encoding='utf-8').splitlines()
    assert len(records) == 1
    record = json.loads(records[0])
    assert record['clock_quality'] == 'unverified'
    assert record['device_observation']['quality']['status'] == 'invalid'
    assert 'parse_error' in record['device_observation']['quality']['reasons']


def test_replay_parse_error_does_not_echo_malformed_input(tmp_path):
    path = tmp_path/'invalid.jsonl'
    sentinel = 'synthetic-private-marker'
    path.write_text(json.dumps({'unexpected':sentinel})+'\n',encoding='utf-8')
    with pytest.raises(ValueError) as error:
        ReplayAdapter.from_file(path)
    assert sentinel not in str(error.value)
    assert str(error.value) == 'invalid replay observation JSON'
