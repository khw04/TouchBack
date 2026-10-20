"""Exercise the server, never construct a verdict or call a real model directly."""
from datetime import datetime, timezone
from time import monotonic
from urllib.parse import quote, urlsplit

import httpx

from app.adapters.demo_setup import SCENARIOS
from app.contracts.models import AckResponse, CancelResponse, CreateRunResponse, RunResponse

TERMINAL = {"succeeded", "uncertain", "failed", "stopped"}
EXPECTED = {name: "uncertain" for name in SCENARIOS}
EXPECTED.update({name: "succeeded" for name in ("normal", "recovery", "delayed", "duplicate")})


class RehearsalError(ValueError):
    pass


def validate_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "::1", "localhost")
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise RehearsalError("use an HTTP loopback origin without credentials, path or query")
    # Trigger port validation before making a request.
    _ = parsed.port
    return value.rstrip("/")


def _json(client, method, path, **kwargs):
    response = client.request(method, path, **kwargs)
    if response.status_code >= 300:
        raise RehearsalError(f"HTTP {response.status_code} at {path}")
    return response.json()


def _check_metadata(data):
    if isinstance(data,dict) and data.get("virtual_cooktop") is True:
        raise RehearsalError("virtual cooktop is interactive; use its browser buttons or restart in scripted demo mode")
    if (not isinstance(data, dict) or data.get("enabled") is not True or data.get("source") != "mock"
            or data.get("execution_mode") != "mock_llm"
            or data.get("physical_validation_performed") is not False
            or data.get("scenario") not in SCENARIOS
            or not isinstance(data.get("scenarios"), list)
            or any(name not in data["scenarios"] for name in SCENARIOS)):
        raise RehearsalError("requires enabled local demo with mock_llm; real model calls are excluded")
    return data


def _metadata(client):
    return _check_metadata(_json(client, "GET", "/api/demo"))


def _snapshot(client, run_id):
    run = RunResponse.model_validate(_json(client, "GET", "/api/runs/" + quote(run_id, safe="")))
    if run.run_id != run_id or run.source != "mock" or run.execution_mode != "mock_llm":
        raise RehearsalError("run identity, observation source or model mode changed")
    return run


def run_rehearsal(client, *, scenarios=("normal", "recovery", "sensor_error"), zone="right", repeat=1, on_case=None):
    """Client has finite transport deadlines. No POST is retried after an unknown response."""
    if zone not in ("right", "left") or type(repeat) is not int or not 1 <= repeat <= 3:
        raise RehearsalError("zone must be right/left and repeat must be 1..3")
    if not scenarios or len(scenarios) > len(SCENARIOS) or any(name not in SCENARIOS for name in scenarios):
        raise RehearsalError("unsupported scenario list")
    initial = _metadata(client)
    report = {"schema_version": "1", "kind": "sensorless_rehearsal", "source": "mock",
              "execution_mode": "mock_llm", "physical_validation_performed": False,
              "started_at": datetime.now(timezone.utc).isoformat(), "cases": [], "settings_restored": False}
    abort = False
    for iteration in range(1, repeat + 1):
        for scenario in scenarios:
            started = monotonic()
            case = {"scenario": scenario, "iteration": iteration, "zone": zone, "run_id": None,
                    "expected_run_status": EXPECTED[scenario],
                    "expected_action_count": 2 if scenario in ("recovery", "recovery_exhausted") else 1,
                    "outcome": "error", "actions": [],
                    "snapshot": None, "cleanup_confirmed": False, "error": None}
            create_sent = False
            try:
                selected = _check_metadata(_json(client, "POST", "/api/demo/scenario", json={"scenario": scenario}))
                if selected.get("scenario") != scenario or selected.get("execution_mode") != "mock_llm":
                    raise RehearsalError("operator selection not confirmed")
                create_sent = True
                created = CreateRunResponse.model_validate(_json(client, "POST", "/api/runs", json={
                    "user_input": "화력을 한 단계 올려줘", "source": "mock", "zone_id": zone}))
                case["run_id"] = created.run_id
                path = "/api/runs/" + quote(created.run_id, safe="")
                run = _snapshot(client, created.run_id)
                for _ in range(8):
                    if run.status in TERMINAL:
                        break
                    if run.status == "awaiting_user":
                        action = run.instruction.action_id if run.instruction else None
                        if not action or action in case["actions"] or len(case["actions"]) >= 2:
                            raise RehearsalError("duplicate/missing action or action budget exhausted")
                        case["actions"].append(action)
                        ack = AckResponse.model_validate(_json(client, "POST", path + "/ack", json={"action_id": action}))
                        if ack.run_id != created.run_id:
                            raise RehearsalError("ack run mismatch")
                        _json(client, "POST", path + "/step", json={"action_id": action})
                    elif run.status not in ("planning", "observing", "recovering"):
                        raise RehearsalError("unexpected clarification or execution state")
                    run = _snapshot(client, created.run_id)
                if run.status not in TERMINAL:
                    raise RehearsalError("bounded observation budget exhausted")
                case["snapshot"] = run.model_dump(mode="json")
                success_supported = run.status != "succeeded" or (run.verdict is not None
                    and run.verdict.status == "success" and run.verdict.verification_level == "exact_step"
                    and run.verdict.goal_satisfied is True and bool(run.verdict.evidence_ids))
                case["outcome"] = ("matched" if run.status == EXPECTED[scenario] and success_supported
                    and len(case["actions"]) == case["expected_action_count"] else "unexpected")
                case["cleanup_confirmed"] = True
            except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError) as error:
                # Store no request headers, API keys or provider response bodies.
                case["error"] = type(error).__name__
                if case["run_id"]:
                    try:
                        path = "/api/runs/" + quote(case["run_id"], safe="")
                        cancelled = CancelResponse.model_validate(_json(client, "POST", path + "/cancel"))
                        run = _snapshot(client, case["run_id"])
                        case["snapshot"] = run.model_dump(mode="json")
                        case["cleanup_confirmed"] = (cancelled.run_id == run.run_id
                            and cancelled.status in TERMINAL and run.status in TERMINAL)
                    except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
                        pass
                # An unknown create could be running. Never create another or cancel somebody else's run.
                abort = not case["cleanup_confirmed"] and create_sent
                if not create_sent:
                    abort = True
            case["elapsed_ms"] = round((monotonic() - started) * 1000, 2)
            report["cases"].append(case)
            if on_case:
                on_case(case)
            if abort:
                break
        if abort:
            break
    if not abort:
        try:
            restored = _check_metadata(_json(client, "POST", "/api/demo/scenario", json={"scenario": initial["scenario"]}))
            report["settings_restored"] = restored.get("scenario") == initial["scenario"]
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
            pass
    report["status"] = ("blocked" if abort else "passed" if report["settings_restored"]
                        and all(case["outcome"] == "matched" for case in report["cases"]) else "failed")
    return report
