"""Synthetic offline assessment fixtures; these are not physical measurements."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from app.contracts.models import Goal, Verdict
from app.evaluation.cli import load_jsonl
from app.evaluation.report import Outcome, ReferenceTrial, evaluate


def trial(n=1, **changes):
    data = dict(trial_id=f"trial-{n}",run_id=f"run-{n}",action_id=f"action-{n}",split="evaluation",
                source="mock",execution_mode="mock_llm",condition_id="synthetic-install-1",calibration_id="mock-test-v1",
                goal=Goal(zone_id="right",direction="increase",requested_steps=1),reference_source="mock_fixture",
                before_level=3,after_level=4)
    data.update(changes)
    return ReferenceTrial(**data)


def outcome(ref, status="succeeded", verdict=None, **changes):
    if verdict is None and status == "succeeded":
        verdict = Verdict(status="success",verification_level="exact_step",goal_satisfied=True,
                          reason_codes=[],evidence_ids=["synthetic-observation"])
    data = dict(run_id=ref.run_id,action_id=ref.action_id,source=ref.source,execution_mode=ref.execution_mode,
                goal=ref.goal,status=status,verdict=verdict,latency_ms=100.0)
    data.update(changes)
    return Outcome(**data)


def test_failures_unknown_labels_missing_results_and_exclusions_are_visible():
    refs = [trial(1),trial(2,after_level=3),trial(3),trial(4),trial(5,before_level=None,after_level=None),
            trial(6,before_level=None,after_level=None),trial(7,exclusion_reason="maximum level condition"),
            trial(8,after_level=5)]
    uncertain = Verdict(status="uncertain",verification_level="none",goal_satisfied=None,reason_codes=["missing"],evidence_ids=[])
    mismatch = Verdict(status="mismatch",verification_level="exact_step",goal_satisfied=False,reason_codes=[],evidence_ids=["synthetic"])
    results = [outcome(refs[0]),outcome(refs[1]),outcome(refs[2],status="uncertain",verdict=uncertain),
               outcome(refs[4],status="failed",latency_ms=None),outcome(refs[5]),outcome(refs[6]),
               outcome(refs[7],status="uncertain",verdict=mismatch)]
    report = evaluate(refs,results)
    group = report["groups"][0]
    assert group["counts"] == dict(total=8,excluded=1,included=7,missing_outcomes=1,unassessable_references=2,
                                  confirmed_successes=3,reference_goal_met=3,correct_confirmations=1,false_successes=1,
                                  unassessable_successes=1,exact_goal_assessed=3,exact_goal_correct=2)
    assert group["rates"]["goal_confirmation"] == pytest.approx(1/3)
    assert group["rates"]["false_success_per_included"] == pytest.approx(1/7)
    assert group["rates"]["uncertain_per_included"] == pytest.approx(2/7)
    assert group["rates"]["exact_goal_agreement"] == pytest.approx(2/3)
    assert group["reported_latency_ms"]["count"] == 5
    assert len(group["trials"]) == 8
    assert group["trials"][3]["status"] == "missing"
    assert report["physical_validation_performed"] is False


def test_empty_denominators_are_null_not_perfect_results():
    ref = trial(exclusion_reason="not an applicable +1 condition")
    group = evaluate([ref],[])["groups"][0]
    assert group["counts"]["total"] == 1 and group["counts"]["included"] == 0
    assert all(value is None for value in group["rates"].values())
    assert group["reported_latency_ms"]["mean"] is None


def test_modes_sources_conditions_calibrations_zones_and_reference_sources_are_not_mixed():
    refs = [trial(1),trial(2,source="replay"),
            trial(3,source="live",reference_source="manual_display",execution_mode="real_llm"),
            trial(4,condition_id="another-installation"),trial(5,calibration_id="another-profile"),
            trial(6,goal=Goal(zone_id="left",direction="increase",requested_steps=1)),
            trial(7,reference_source="video_display"),trial(8,execution_mode="backend_stub")]
    report = evaluate(refs,[])
    assert len(report["groups"]) == 8
    assert all(g["counts"]["missing_outcomes"] == 1 for g in report["groups"])


@pytest.mark.parametrize("which", ["trial_id","action_key","outcome_key","extra_outcome"])
def test_duplicate_or_unmatched_identity_is_rejected(which):
    one = trial(1); two = trial(2)
    if which == "trial_id":
        two.trial_id = one.trial_id
        args = ([one,two],[])
    elif which == "action_key":
        two.run_id,two.action_id=one.run_id,one.action_id
        args = ([one,two],[])
    elif which == "outcome_key":
        args = ([one],[outcome(one),outcome(one)])
    else:
        args = ([one],[outcome(two)])
    with pytest.raises(ValueError):
        evaluate(*args)


@pytest.mark.parametrize("change", [{"source":"replay"},{"execution_mode":"real_llm"},
                                   {"goal":Goal(zone_id="left",direction="increase",requested_steps=1)}])
def test_reference_and_result_scope_must_match(change):
    ref=trial()
    with pytest.raises(ValueError):
        evaluate([ref],[outcome(ref,**change)])


def test_no_reference_is_unassessable_and_development_cannot_leak_into_evaluation():
    unknown=trial(reference_source="unavailable",before_level=None,after_level=None)
    group=evaluate([unknown],[outcome(unknown)])["groups"][0]
    assert group["counts"]["unassessable_successes"] == 1
    assert group["rates"]["goal_confirmation"] is None
    with pytest.raises(ValueError): trial(split="development")
    with pytest.raises(ValueError): trial(source="live",reference_source="mock_fixture")
    with pytest.raises(ValueError): trial(reference_source="unavailable")


@pytest.mark.parametrize("bad", [-1,float("nan"),float("inf"),True])
def test_invalid_reported_latency_rejected(bad):
    with pytest.raises(ValueError): outcome(trial(),latency_ms=bad)


def test_inconsistent_or_evidenceless_success_is_rejected():
    ref=trial()
    valid=outcome(ref)
    valid.verdict.evidence_ids=[]
    with pytest.raises(ValueError): evaluate([ref],[valid])
    with pytest.raises(ValueError): outcome(trial(execution_mode="backend_stub"))
    with pytest.raises(ValueError): outcome(ref,status="failed",verdict=outcome(ref).verdict)
    data=outcome(ref).model_dump();data["verdict"]=None
    with pytest.raises(ValueError): Outcome(**data)


@pytest.mark.parametrize("payload", [b'{"trial_id":"a","trial_id":"b"}\n',b'\xff\n',b'{}',b'x'*65537+b'\n',b'{}\n'], ids=["duplicate-key","bad-utf8","missing-lf","oversize","wrong-schema"])
def test_bad_jsonl_never_skips_invalid_lines(tmp_path,payload):
    path=tmp_path / "bad.jsonl";path.write_bytes(payload)
    with pytest.raises((ValueError,UnicodeError)):
        load_jsonl(path,ReferenceTrial)


def test_cli_real_process_and_no_overwrite_or_secret_echo(tmp_path):
    ref=trial();result=outcome(ref)
    references=tmp_path / "references.jsonl";outputs=tmp_path / "outcomes.jsonl";report=tmp_path / "report.json"
    references.write_text(ref.model_dump_json()+"\n",encoding="utf-8")
    outputs.write_text(result.model_dump_json()+"\n",encoding="utf-8")
    backend=Path(__file__).resolve().parents[1]
    args=[sys.executable,"-m","app.evaluation.cli","--references",str(references),"--outcomes",str(outputs),"--output",str(report)]
    completed=subprocess.run(args,cwd=backend,text=True,capture_output=True)
    assert completed.returncode == 0,completed.stderr
    assert json.loads(report.read_text(encoding="utf-8"))["groups"][0]["counts"]["correct_confirmations"] == 1
    original=report.read_bytes()
    assert subprocess.run(args,cwd=backend,capture_output=True).returncode == 2
    assert report.read_bytes()==original
    references.write_text('{"private diagnostic":"do not echo"}\n',encoding="utf-8")
    next_report=tmp_path / "next.json"
    invalid=subprocess.run(args[:-1]+[str(next_report)],cwd=backend,text=True,capture_output=True)
    assert invalid.returncode == 2 and not next_report.exists()
    assert "do not echo" not in invalid.stderr


def test_empty_outcome_file_is_reported_as_missing_not_excluded(tmp_path):
    path=tmp_path / "empty.jsonl";path.write_bytes(b"")
    ref=trial()
    report=evaluate([ref],load_jsonl(path,Outcome))
    assert report["groups"][0]["counts"]["included"] == 1
    assert report["groups"][0]["counts"]["missing_outcomes"] == 1
    assert report["groups"][0]["rates"]["goal_confirmation"] == 0

def test_empty_evidence_identifier_is_not_a_verified_success():
    ref=trial();result=outcome(ref)
    result.verdict.evidence_ids=[" "]
    with pytest.raises(ValueError): evaluate([ref],[result])


def test_committed_synthetic_example_is_executable():
    root=Path(__file__).resolve().parents[2]
    refs=load_jsonl(root / "docs/examples/evaluation/mock-references.jsonl",ReferenceTrial)
    results=load_jsonl(root / "docs/examples/evaluation/mock-outcomes.jsonl",Outcome)
    group=evaluate(refs,results)["groups"][0]
    assert group["counts"]["total"] == 3
    assert group["counts"]["missing_outcomes"] == 1
    assert group["rates"]["goal_confirmation"] == .5
    assert group["rates"]["uncertain_per_included"] == pytest.approx(1/3)


@pytest.mark.parametrize("zone", ["left", "right"])
@pytest.mark.parametrize("direction,after,met", [
    ("increase", 6, True), ("increase", 4, False),
    ("decrease", 4, True), ("decrease", 6, False),
])
def test_evaluation_success_matches_requested_direction(zone, direction, after, met):
    ref = trial(goal=Goal(zone_id=zone, direction=direction, requested_steps=1), before_level=5, after_level=after)
    group = evaluate([ref], [outcome(ref)])["groups"][0]
    assert group["trials"][0]["reference_goal_met"] is met
    assert group["counts"]["correct_confirmations"] == int(met)
    assert group["counts"]["false_successes"] == int(not met)
    assert group["counts"]["exact_goal_correct"] == int(met)
    assert group["rates"]["false_success_per_included"] == int(not met)


@pytest.mark.parametrize("direction", ["increase", "decrease"])
def test_directional_reference_with_missing_level_stays_unassessable(direction):
    ref = trial(goal=Goal(zone_id="right", direction=direction, requested_steps=1), before_level=5, after_level=None)
    group = evaluate([ref], [outcome(ref)])["groups"][0]
    assert group["trials"][0]["reference_goal_met"] is None
    assert group["counts"]["unassessable_successes"] == 1
    assert group["counts"]["correct_confirmations"] == group["counts"]["false_successes"] == 0
