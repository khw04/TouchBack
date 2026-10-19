"""Offline CLI: references and verdicts stay out of Agent/adapter input."""
import argparse
import json
from pathlib import Path

from app.adapters.serial_input import unique_object
from app.evaluation.report import Outcome, ReferenceTrial, evaluate


def load_jsonl(path, model):
    records = []
    with Path(path).open("rb") as stream:
        while line := stream.readline(65537):
            if len(line) > 65536 or not line.endswith(b"\n") or len(records) >= 10000:
                raise ValueError("input line/record limit exceeded or missing LF")
            data = json.loads(line.decode("utf-8"), object_pairs_hook=unique_object)
            records.append(model.model_validate_json(json.dumps(data)))
    return records


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate separately stored independent references and terminal verdicts")
    parser.add_argument("--references", type=Path, required=True)
    parser.add_argument("--outcomes", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = evaluate(load_jsonl(args.references, ReferenceTrial), load_jsonl(args.outcomes, Outcome))
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    except (OSError, ValueError, UnicodeError):
        parser.exit(2, "Evaluation failed; check input contracts/IDs and choose a new output path.\n")
    print(f"Report generated: {report['input_trials']} trials, {len(report['groups'])} separate groups; not an acceptance or physical-validation result.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
