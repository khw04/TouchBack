"""Join separately stored references and verdicts without dropping unsuccessful trials."""
from collections import Counter, defaultdict
from typing import Annotated, Literal
from statistics import mean
from pydantic import Field, model_validator

from app.contracts.models import Contract, ExecutionMode, Goal, NonNegativeInt, Source, Text, Verdict


class ReferenceTrial(Contract):
    trial_id: Text
    run_id: Text
    action_id: Text
    split: Literal["evaluation"]
    source: Source
    execution_mode: ExecutionMode
    condition_id: Text
    calibration_id: Text
    goal: Goal
    reference_source: Literal["manual_display", "video_display", "mock_fixture", "unavailable"]
    before_level: NonNegativeInt | None
    after_level: NonNegativeInt | None
    exclusion_reason: Text | None = None

    @model_validator(mode="after")
    def check_reference(self):
        if self.source == "live" and self.reference_source == "mock_fixture":
            raise ValueError("live evaluation requires an independent physical reference")
        if self.reference_source == "unavailable" and (self.before_level is not None or self.after_level is not None):
            raise ValueError("unavailable reference must not contain inferred levels")
        return self


class Outcome(Contract):
    run_id: Text
    action_id: Text
    source: Source
    execution_mode: ExecutionMode
    goal: Goal
    status: Literal["succeeded", "uncertain", "stopped", "failed"]
    verdict: Verdict | None
    latency_ms: Annotated[float, Field(ge=0)] | None

    @model_validator(mode="after")
    def check_success(self):
        success = self.verdict is not None and self.verdict.status == "success"
        if success != (self.status == "succeeded"):
            raise ValueError("terminal status and success verdict disagree")
        if success and (not self.verdict.evidence_ids or any(not item.strip() for item in self.verdict.evidence_ids)):
            raise ValueError("a success claim requires evidence IDs")
        if success and self.execution_mode == "backend_stub":
            raise ValueError("backend_stub cannot claim verified success")
        return self


def ratio(numerator, denominator):
    return None if not denominator else numerator / denominator


def evaluate(trials: list[ReferenceTrial], outcomes: list[Outcome]) -> dict:
    if not trials:
        raise ValueError("evaluation trials are required")
    # Revalidate callers' models as well as JSON input; don't trust model_construct/mutation.
    trials = [ReferenceTrial.model_validate_json(item.model_dump_json()) for item in trials]
    outcomes = [Outcome.model_validate_json(item.model_dump_json()) for item in outcomes]
    trial_ids = [item.trial_id for item in trials]
    trial_keys = [(item.run_id, item.action_id) for item in trials]
    if len(set(trial_ids)) != len(trial_ids) or len(set(trial_keys)) != len(trial_keys):
        raise ValueError("duplicate trial ID or run/action reference")
    result_map = {}
    for outcome in outcomes:
        key = (outcome.run_id, outcome.action_id)
        if key in result_map:
            raise ValueError("duplicate outcome for run/action")
        result_map[key] = outcome
    if set(result_map) - set(trial_keys):
        raise ValueError("unmatched outcomes must not be silently discarded")
    groups = defaultdict(list)
    for trial in trials:
        outcome = result_map.get((trial.run_id, trial.action_id))
        if outcome and (outcome.source != trial.source or outcome.execution_mode != trial.execution_mode or outcome.goal != trial.goal):
            raise ValueError("reference/outcome source, execution mode or goal mismatch")
        key = (trial.source, trial.execution_mode, trial.condition_id, trial.calibration_id,
               trial.goal.zone_id, trial.reference_source)
        groups[key].append((trial, outcome))
    reports = []
    for key, entries in sorted(groups.items()):
        stats = Counter()
        statuses = Counter()
        levels = Counter()
        latencies = []
        rows = []
        for trial, outcome in entries:
            included = trial.exclusion_reason is None
            known = trial.reference_source != "unavailable" and trial.before_level is not None and trial.after_level is not None
            expected = trial.after_level - trial.before_level == trial.goal.requested_steps if known else None
            confirmed = outcome is not None and outcome.status == "succeeded"
            row = {"trial_id": trial.trial_id, "run_id": trial.run_id, "action_id": trial.action_id,
                   "included": included, "exclusion_reason": trial.exclusion_reason,
                   "before_level": trial.before_level, "after_level": trial.after_level,
                   "requested_steps": trial.goal.requested_steps, "reference_goal_met": expected,
                   "status": outcome.status if outcome else "missing",
                   "verdict_status": outcome.verdict.status if outcome and outcome.verdict else None,
                   "verification_level": outcome.verdict.verification_level if outcome and outcome.verdict else None,
                   "latency_ms": outcome.latency_ms if outcome else None,
                   "reason_codes": outcome.verdict.reason_codes if outcome and outcome.verdict else [],
                   "evidence_ids": outcome.verdict.evidence_ids if outcome and outcome.verdict else []}
            rows.append(row)
            stats["total"] += 1
            if not included:
                stats["excluded"] += 1
                continue
            stats["included"] += 1
            statuses[row["status"]] += 1
            levels[row["verification_level"] or "unreported"] += 1
            stats["missing_outcomes"] += outcome is None
            stats["unassessable_references"] += not known
            stats["confirmed_successes"] += confirmed
            stats["reference_goal_met"] += expected is True
            stats["correct_confirmations"] += confirmed and expected is True
            stats["false_successes"] += confirmed and expected is False
            stats["unassessable_successes"] += confirmed and expected is None
            if known and outcome and outcome.verdict and outcome.verdict.verification_level == "exact_step":
                stats["exact_goal_assessed"] += 1
                stats["exact_goal_correct"] += outcome.verdict.goal_satisfied == expected
            if outcome and outcome.latency_ms is not None:
                latencies.append(outcome.latency_ms)
        reports.append({
            "source": key[0], "execution_mode": key[1], "condition_id": key[2],
            "calibration_id": key[3], "zone_id": key[4], "reference_source": key[5],
            "counts": {name: stats[name] for name in ("total","excluded","included","missing_outcomes",
                       "unassessable_references","confirmed_successes","reference_goal_met","correct_confirmations",
                       "false_successes","unassessable_successes","exact_goal_assessed","exact_goal_correct")},
            "status_counts": dict(sorted(statuses.items())), "verification_level_counts": dict(sorted(levels.items())),
            "rates": {"goal_confirmation": ratio(stats["correct_confirmations"], stats["reference_goal_met"]),
                      "false_success_per_included": ratio(stats["false_successes"], stats["included"]),
                      "uncertain_per_included": ratio(statuses["uncertain"], stats["included"]),
                      "exact_goal_agreement": ratio(stats["exact_goal_correct"], stats["exact_goal_assessed"])},
            "reported_latency_ms": {"count": len(latencies), "min": min(latencies) if latencies else None,
                                    "max": max(latencies) if latencies else None,
                                    "mean": mean(latencies) if latencies else None},
            "trials": rows,
        })
    return {"schema_version": "touchback-evaluation-v1", "split": "evaluation",
            "input_trials": len(trials), "input_outcomes": len(outcomes), "groups": reports,
            "physical_validation_performed": False,
            "limitations": ["Input references and reported latency provenance are supplied by the operator, not independently verified by this tool.",
                            "Goal agreement is not absolute heating-level accuracy or a physical performance guarantee.",
                            "Missing and uncertain outcomes remain in denominators; exclusions are listed explicitly."]}
