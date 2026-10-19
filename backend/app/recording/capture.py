"""Read-only serial capture to replay-compatible observations and explicit error events."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time

from app.adapters.serial_input import SerialAdapter


def capture(port: str, output_dir: Path, *, duration_seconds: float = 30,
            baudrate: int = 115200, expected_device_id: str | None = None,
            adapter_factory=SerialAdapter.open, monotonic=time.monotonic) -> dict:
    if not port.strip() or type(baudrate) is not int or baudrate <= 0:
        raise ValueError("port and positive baudrate are required")
    if isinstance(duration_seconds, bool) or not math.isfinite(duration_seconds) or not 0 < duration_seconds <= 3600:
        raise ValueError("duration must be finite, positive and at most 3600 seconds")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    counts = Counter()
    records = 0
    closing = False
    status = "failed"
    started_at = datetime.now(timezone.utc).isoformat()
    start = monotonic()
    adapter = None
    with (output_dir / "observations.jsonl").open("x", encoding="utf-8", newline="\n") as observations, \
         (output_dir / "events.jsonl").open("x", encoding="utf-8", newline="\n") as events:
        def error(reason):
            if closing and reason == "disconnected":
                return  # Normal close is not a transport failure.
            counts[reason] += 1
            events.write(json.dumps({"kind": "error", "reason": reason,
                                     "elapsed_seconds": max(0, monotonic() - start)}, allow_nan=False) + "\n")
            events.flush()

        def record(observation):
            nonlocal records
            encoded = observation.model_dump_json() + "\n"
            if len(encoded.encode("utf-8")) > 8192:
                error("host_record_too_long")
                return
            observations.write(encoded)
            observations.flush()
            records += 1

        try:
            adapter = adapter_factory(port, baudrate=baudrate, expected_device_id=expected_device_id,
                                      on_observation=record, on_error=error)
            while monotonic() - start < duration_seconds and not adapter.closed:
                adapter.poll()
                time.sleep(.001)
            status = "disconnected" if adapter.closed else "completed"
        except KeyboardInterrupt:
            status = "interrupted"
        except Exception:
            # Preserve a machine-readable failure, not arbitrary serial/OS exception contents.
            error("capture_failed")
            status = "failed"
        finally:
            closing = True
            if adapter is not None:
                try:
                    adapter.close()
                except Exception:
                    error("close_failed")
                    status = "failed"
            if not records:
                error("no_observations")
            summary = {
                "schema_version": "touchback-capture-v1", "source": "live",
                "clock_quality": "unverified", "status": status,
                "started_at": started_at, "ended_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_seconds": max(0, monotonic() - start), "baudrate": baudrate,
                "observations": records, "error_count": sum(counts.values()),
                "errors": dict(sorted(counts.items())),
                "physical_validation_performed": False,
            }
            with (output_dir / "summary.json").open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(summary, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Capture live v0.2 serial frames without sending commands")
    parser.add_argument("--port", required=True)
    parser.add_argument("--output-dir", type=Path, required=True, help="New directory; existing paths are never overwritten")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--device-id")
    args = parser.parse_args(argv)
    try:
        result = capture(args.port, args.output_dir, duration_seconds=args.duration,
                         baudrate=args.baudrate, expected_device_id=args.device_id)
    except (OSError, ValueError):
        parser.exit(2, "Capture could not start; check port, options and use a new output directory.\n")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "completed" and not result["error_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
