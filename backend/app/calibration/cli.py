"""Offline candidate generation. Input labels never go to the Agent."""
import argparse
from pathlib import Path
from app.calibration.fitting import DevelopmentSample, fit_candidate


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate draft calibration from development data")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--calibration-id", required=True)
    parser.add_argument("--margin", type=float, default=0.0)
    args = parser.parse_args()
    samples = []
    with args.input.open("rb") as stream:
        while line := stream.readline(65537):
            if len(line) > 65536 or not line.endswith(b"\n") or len(samples) >= 10000:
                raise ValueError("invalid or excessive development input")
            samples.append(DevelopmentSample.model_validate_json(line))
    candidate = fit_candidate(samples, calibration_id=args.calibration_id, margin=args.margin)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(candidate.model_dump_json(indent=2) + "\n")
    print(f"Draft candidate generated ({candidate.source}); physical validation/activation not performed.")


if __name__ == "__main__":
    main()
