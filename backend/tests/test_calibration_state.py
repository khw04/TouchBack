import pytest
from app.adapters.mock_setup import build_mock_adapter
from app.calibration.fitting import DevelopmentSample, fit_candidate
from app.observations.state import StateTracker, ZoneState
from app.contracts.models import Goal
from app.verification.models import ActionContext


def samples(zone="right", session="mock-session-01"):
    observation=build_mock_adapter(zone_id=zone).read_baseline(zone).device_observation
    result=[]
    for level,rms in [(1,100.0),(2,120.0),(3,140.0)]:
        for index in range(3):
            device=observation.model_copy(deep=True)
            device.seq=(level-1)*3+index
            device.session_id=session
            device.field_summary.rms=rms+index-1
            result.append(DevelopmentSample(trial_id=f"trial-{level}-{index}",split="development",reference_source="mock_fixture",displayed_level=level,
                cooktop_model="mock-model",cookware_id="mock-pot",installation_id="mock-install",observation=device))
    return result


def profile(zone="right", session="mock-session-01"):
    return fit_candidate(samples(zone,session),calibration_id="mock-test-v1",margin=1.0)


def prepared():
    adapter=build_mock_adapter()
    state=ZoneState("right")
    state.activate_mock(profile(),installation_id="mock-install")
    baseline=adapter.read_baseline("right")
    assert state.observe(baseline,now_ms=500)
    return state,adapter,baseline


def context():
    return ActionContext(run_id="run-1",action_id="action-1",window_start_ms=500,window_end_ms=3500,now_ms=3500)


def goal():return Goal(zone_id="right",direction="increase",requested_steps=1)


def test_fitting_candidate_retains_scope_and_development_provenance():
    candidate=profile()
    assert candidate.status=="draft" and candidate.source=="mock"
    assert [(s.level,s.rms_min,s.rms_max) for s in candidate.signatures]==[(1,98.0,102.0),(2,118.0,122.0),(3,138.0,142.0)]
    assert len(candidate.trial_ids)==9


@pytest.mark.parametrize("failure",["evaluation","duplicate_trial","duplicate_observation","mixed_installation","mixed_session","bad_quality","field_missing","partial_samples","too_few","overlap","nonmonotonic","live_fake_label"])
def test_invalid_development_data_cannot_generate_calibration(failure):
    data=samples()
    if failure=="evaluation":data[0].split="evaluation"
    elif failure=="duplicate_trial":data[0].trial_id=data[1].trial_id
    elif failure=="duplicate_observation":data[0].observation.seq=data[1].observation.seq
    elif failure=="mixed_installation":data[0].installation_id="other"
    elif failure=="mixed_session":data[0].observation.session_id="other"
    elif failure=="bad_quality":data[0].observation.quality.status="invalid"
    elif failure=="field_missing":data[0].observation.field_summary=None
    elif failure=="partial_samples":data[0].observation.field_summary.sample_count=1
    elif failure=="too_few":data.pop()
    elif failure=="overlap":
        for item in data:item.observation.field_summary.rms=100.0
    elif failure=="nonmonotonic":
        for item in data:item.observation.field_summary.rms=200.0-item.displayed_level*20.0
    elif failure=="live_fake_label":
        for item in data:item.observation.source="live"
    with pytest.raises(ValueError):fit_candidate(data,calibration_id="candidate")


@pytest.mark.parametrize("kwargs",[{"margin":-1.0},{"margin":float("nan")},{"margin":float("inf")},{"min_per_level":2}])
def test_invalid_fitting_settings_rejected(kwargs):
    with pytest.raises(ValueError):fit_candidate(samples(),calibration_id="candidate",**kwargs)


def test_ready_state_verifies_transition_then_tracks_observed_level():
    state,adapter,_=prepared()
    after=adapter.await_touch_result(run_id="run-1",action_id="action-1",zone_id="right",window_start_ms=500,window_end_ms=3500)
    result=state.verify(goal(),after,context())
    assert result.status=="success" and result.verification_level=="exact_step"
    assert state.level==1  # verdict alone does not mutate observed physical state
    assert state.observe(after[0],now_ms=3500)
    assert state.snapshot()["level"]==2
    assert "trial_ids" not in state.snapshot()


def test_reboot_requires_recalibration_and_fresh_baseline_before_resuming():
    state,_,baseline=prepared()
    reboot=baseline.model_copy(deep=True)
    reboot.device_observation.session_id="boot-2"
    reboot.observation_id="mock-device-01:boot-2:4"
    assert not state.observe(reboot,now_ms=500)
    assert state.level is None and state.status=="needs_calibration"
    assert state.verify(goal(),[],context()).status=="uncertain"
    state.activate_mock(profile(session="boot-2"),installation_id="mock-install")
    assert state.status=="needs_baseline"
    assert state.verify(goal(),[],context()).status=="uncertain"
    assert state.observe(reboot,now_ms=500)
    assert state.status=="ready"


@pytest.mark.parametrize("reason",["disconnected","saturated","clock","stale","unclassified","settings"])
def test_sensor_and_state_problems_invalidate_calibration(reason):
    state,_,baseline=prepared()
    changed=baseline.model_copy(deep=True)
    if reason in ("disconnected","saturated"):changed.device_observation.quality.reasons=[reason]
    elif reason=="clock":changed.clock_quality="unverified"
    elif reason=="unclassified":changed.device_observation.field_summary.rms=111.0
    elif reason=="settings":changed.device_observation.field_summary.unit="millivolt"
    if reason=="unclassified":
        changed.device_observation.seq=5;changed.observation_id="mock-device-01:mock-session-01:5"
    assert not state.observe(changed,now_ms=6000 if reason=="stale" else 500)
    assert state.level is None and state.status=="needs_calibration"


def test_right_and_left_profiles_are_isolated():
    tracker=StateTracker()
    tracker.zones["right"].activate_mock(profile(),installation_id="mock-install")
    tracker.zones["left"].activate_mock(profile("left"),installation_id="mock-install")
    assert tracker.zones["right"].observe(build_mock_adapter().read_baseline("right"),now_ms=500)
    assert tracker.zones["left"].observe(build_mock_adapter(zone_id="left").read_baseline("left"),now_ms=500)
    tracker.zones["right"].invalidate("sensor_error")
    assert tracker.zones["left"].status=="ready"
    tracker.invalidate_all("device_reboot")
    assert all(zone.level is None for zone in tracker.zones.values())


def test_live_candidate_is_draft_only_and_cannot_activate():
    data=samples()
    for item in data:
        item.observation.source="live";item.reference_source="manual_display"
    candidate=fit_candidate(data,calibration_id="synthetic-live-labelled-draft")
    assert candidate.status=="draft"
    with pytest.raises(ValueError):ZoneState("right").activate_mock(candidate,installation_id="mock-install")


def test_wrong_installation_or_zone_cannot_activate():
    with pytest.raises(ValueError):ZoneState("right").activate_mock(profile(),installation_id="other")
    with pytest.raises(ValueError):ZoneState("left").activate_mock(profile(),installation_id="mock-install")


def test_stale_duplicate_does_not_keep_state_ready():
    state,_,baseline=prepared()
    assert not state.observe(baseline,now_ms=6000)
    assert state.status=="needs_calibration"


def test_other_zone_observation_does_not_erase_valid_state():
    state,_,_=prepared()
    assert not state.observe(build_mock_adapter(zone_id="left").read_baseline("left"),now_ms=500)
    assert state.status=="ready" and state.level==1


def test_close_signatures_can_be_fitted_but_not_overclaim_step_resolution():
    data=samples()
    for item in data:item.observation.field_summary.rms=100.0+(item.displayed_level-1)*3.0
    candidate=fit_candidate(data,calibration_id="mock-test-v1")
    state=ZoneState("right");state.activate_mock(candidate,installation_id="mock-install")
    adapter=build_mock_adapter();assert state.observe(adapter.read_baseline("right"),now_ms=500)
    after=adapter.await_touch_result(run_id="run-1",action_id="action-1",zone_id="right",window_start_ms=500,window_end_ms=3500)
    assert state.verify(goal(),after,context()).reason_codes==["calibration_resolution_insufficient"]


def test_offline_cli_writes_draft_and_does_not_overwrite(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from app.calibration.fitting import CalibrationCandidate
    path=tmp_path/"development.jsonl"
    output=tmp_path/"candidate.json"
    path.write_text("".join(item.model_dump_json()+"\n" for item in samples()),encoding="utf-8")
    command=[sys.executable,"-m","app.calibration.cli","--input",str(path),"--output",str(output),"--calibration-id","mock-test-v1"]
    backend=Path(__file__).resolve().parents[1]
    first=subprocess.run(command,cwd=backend,capture_output=True,text=True)
    assert first.returncode==0
    assert CalibrationCandidate.model_validate_json(output.read_text(encoding="utf-8")).status=="draft"
    original=output.read_bytes()
    second=subprocess.run(command,cwd=backend,capture_output=True,text=True)
    assert second.returncode!=0 and output.read_bytes()==original


def test_adapter_integrity_fault_invalidates_tracked_states():
    from app.adapters.input_buffer import InputBuffer
    tracker=StateTracker()
    tracker.zones["right"],_,_=prepared()
    buffer=InputBuffer("live")
    buffer.invalidate("disconnected")
    tracker.handle_integrity(buffer.check_sensor_integrity())
    assert tracker.zones["right"].status=="needs_calibration"
    assert tracker.zones["right"].level is None
