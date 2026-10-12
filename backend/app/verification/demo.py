"""Local verifier demo. This is not an LLM Agent or physical-control entry point."""
import argparse
import json
from pathlib import Path

from app.adapters.mock_setup import build_mock_adapter
from app.contracts.models import Goal
from app.verification.models import ActionContext, MockCalibration
from app.verification.verifier import verify_intent


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock-only intent verification")
    parser.add_argument("--scenario", choices=["normal", "no_change", "missing", "delayed", "duplicate", "sensor_error"], default="normal")
    parser.add_argument("--calibration", type=Path, required=True)
    args = parser.parse_args()
    calibration = MockCalibration.model_validate_json(args.calibration.read_text(encoding="utf-8"))
    adapter = build_mock_adapter(args.scenario)
    baseline = adapter.read_baseline("right")
    observations = adapter.await_touch_result(run_id="run-1", action_id="action-1", zone_id="right", window_start_ms=500, window_end_ms=3500)
    result = verify_intent(
        Goal(zone_id="right", direction="increase", requested_steps=1), baseline, observations,
        ActionContext(run_id="run-1", action_id="action-1", window_start_ms=500, window_end_ms=3500, now_ms=adapter.clock.now_ms),
        calibration,
    )
    print(json.dumps({"source": "mock", "verdict": result.model_dump(mode="json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
