from datetime import datetime, timezone
from pathlib import Path
import json
import pytest
import serial

from app.adapters.input_buffer import InputBuffer
from app.adapters.serial_input import JsonLinesDecoder, SerialAdapter
from app.adapters.replay import ReplayAdapter
from app.contracts.models import DeviceObservation, Observation

UTC = datetime(2026, 10, 10, tzinfo=timezone.utc)
EXAMPLE = Path(__file__).resolve().parents[2]/"docs/examples/device-observation.json"


def device(seq=0, session="boot-1", source="live"):
    payload=json.loads(EXAMPLE.read_text(encoding="utf-8"))
    payload.update(seq=seq, session_id=session, source=source, window_start_ms=seq*100, window_end_ms=(seq+1)*100)
    return DeviceObservation.model_validate(payload)


def test_fragmented_multiple_json_lines():
    found, errors = [], []
    decoder=JsonLinesDecoder(found.append, errors.append)
    data=(device().model_dump_json()+"\n"+device(1).model_dump_json()+"\n").encode()
    decoder.feed(data[:17]); assert found == []
    decoder.feed(data[17:])
    assert [item.seq for item in found] == [0,1] and errors == []


@pytest.mark.parametrize("line", [b"garbage\n", b"\xff\n", b"{}\n", b'{"seq":0,"seq":1}\n', b'[]\n'])
def test_bad_lines_are_explicit_errors(line):
    found, errors = [], []
    JsonLinesDecoder(found.append, errors.append).feed(line)
    assert found == [] and errors == ["parse_error"]


def test_oversized_and_unterminated_input_are_bounded():
    found, errors = [], []
    decoder=JsonLinesDecoder(found.append, errors.append)
    decoder.feed(b"x"*10000+b"\n"+device().model_dump_json().encode()+b"\n")
    assert errors == ["line_too_long"] and len(found)==1
    decoder.feed(b"partial"); decoder.finish()
    assert errors[-1] == "incomplete_line"


def test_live_source_mismatch_invalidates_existing_baseline():
    buf=InputBuffer("live")
    buf.ingest(device(),100,UTC)
    buf.ingest(device(1,source="mock"),200,UTC)
    assert buf.read_baseline("right") is None
    assert buf.check_sensor_integrity().reasons == ["source_mismatch"]


def test_five_samples_only_estimate_mapping_and_never_verify_live():
    buf=InputBuffer("live")
    for seq in range(5): buf.ingest(device(seq),(seq+1)*100+1000,UTC)
    result=buf.read_baseline("right")
    assert (result.mapped_window_start_ms,result.mapped_window_end_ms)==(1400,1500)
    assert result.clock_quality == "unverified"
    assert "clock_unverified" in buf.check_sensor_integrity().reasons


def test_duplicates_and_conflicting_duplicate():
    buf=InputBuffer("live")
    buf.ingest(device(),100,UTC);buf.ingest(device(),200,UTC)
    assert len(buf.collect("run","action","right",0,500))==1
    assert buf.collect("run","action","right",0,500)==[]
    changed=device();changed.beep_count=9
    buf.ingest(changed,300,UTC)
    assert buf.check_sensor_integrity().reasons == ["conflicting_duplicate"]


def test_reboot_discards_baseline_and_old_session_cannot_return():
    buf=InputBuffer("live")
    buf.ingest(device(4),500,UTC)
    buf.ingest(device(0,session="boot-2"),600,UTC)
    result=buf.read_baseline("right")
    assert result.device_observation.session_id=="boot-2"
    assert result.device_observation.quality.status=="invalid"
    assert result.mapped_window_end_ms is None
    buf.ingest(device(5),700,UTC)
    assert buf.check_sensor_integrity().reasons==["retired_session"]
    buf.reset();assert buf.read_baseline("right") is None


def test_regression_and_wrong_device_clear_evidence():
    buf=InputBuffer("live")
    buf.ingest(device(2),300,UTC);buf.ingest(device(1),400,UTC)
    assert buf.check_sensor_integrity().reasons==["sequence_regressed"]
    other=device(3);other.device_id="other"
    buf.ingest(other,500,UTC)
    assert buf.check_sensor_integrity().reasons==["device_mismatch"]


def test_real_pyserial_loopback_reads_without_physical_device():
    transport=serial.serial_for_url("loop://",timeout=.01)
    adapter=SerialAdapter(transport,now_ms=lambda:1000,utc_now=lambda:UTC)
    transport.write((device().model_dump_json()+"\n").encode())
    result=adapter.read_baseline("right")
    assert result.device_observation.source=="live"
    assert result.clock_quality=="unverified"
    adapter.close();adapter.close()
    assert adapter.check_sensor_integrity().reasons==["disconnected"]


class BrokenTransport:
    timeout=.01
    def read(self,_): raise OSError("private port detail")
    def close(self): pass


def test_disconnect_is_explicit_and_contains_no_exception_details():
    adapter=SerialAdapter(BrokenTransport())
    assert adapter.read_baseline("right") is None
    assert adapter.check_sensor_integrity().reasons==["disconnected"]


def test_unbounded_read_is_rejected_and_cancellation_discards_input():
    transport=BrokenTransport();transport.timeout=None
    with pytest.raises(ValueError):SerialAdapter(transport)
    adapter=SerialAdapter(BrokenTransport(),now_ms=lambda:1000)
    assert adapter.await_touch_result(run_id="run",action_id="action",zone_id="right",window_start_ms=0,window_end_ms=500,cancelled=lambda:True)==[]
    assert adapter.check_sensor_integrity().reasons==["cancelled"]


def records():
    return [Observation(schema_version="0.2",observation_id=f"esp32-01:boot-1:{seq}",run_id="old-run",action_id="old-action",received_at=UTC,received_monotonic_ms=(seq+1)*100,
        mapped_window_start_ms=seq*100,mapped_window_end_ms=(seq+1)*100,clock_quality="verified",device_observation=device(seq,source="mock")) for seq in range(6)]


def test_replay_source_and_action_ids_are_not_original_run():
    adapter=ReplayAdapter(records())
    assert adapter.read_baseline("right").device_observation.source=="replay"
    assert set(adapter.recorded_sources)=={"mock"}
    results=adapter.await_touch_result(run_id="new-run",action_id="new-action",zone_id="right",window_start_ms=100,window_end_ms=1000,timeout_ms=500)
    assert results and all(item.run_id=="new-run" and item.action_id=="new-action" for item in results)
    assert all(item.clock_quality=="unverified" for item in results)
    assert adapter.await_touch_result(run_id="new-run",action_id="new-action",zone_id="right",window_start_ms=100,window_end_ms=1000,timeout_ms=0)==[]


def test_replay_file_round_trip_and_invalid_order(tmp_path):
    path=tmp_path/"observations.jsonl"
    path.write_text("".join(item.model_dump_json()+"\n" for item in records()),encoding="utf-8")
    assert ReplayAdapter.from_file(path).recorded_sources == tuple(["mock"]*6)
    with pytest.raises(ValueError):ReplayAdapter(list(reversed(records())))


@pytest.mark.parametrize("content", [b"x"*8193+b"\n", b"{}", b"{}\n", b"\xff\n"])
def test_invalid_replay_file_is_not_skipped(tmp_path, content):
    path=tmp_path/"bad.jsonl";path.write_bytes(content)
    with pytest.raises(ValueError):ReplayAdapter.from_file(path)


def test_retained_records_and_diagnostics_are_bounded():
    buf=InputBuffer("live")
    for seq in range(300):buf.ingest(device(seq),(seq+1)*100,UTC)
    assert len(buf._records)==256 and len(buf._seen)==256
    for _ in range(150):buf.invalidate("parse_error")
    assert len(buf.errors)==100
