"""PYTHONPATH=backend python -m app.rehearsal --output logs/rehearsal.jsonl"""
import argparse
import json
from pathlib import Path

import httpx

from app.rehearsal.runner import RehearsalError, run_rehearsal, validate_url


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local Mock-only rehearsal; no physical or real LLM validation")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--zone", choices=("right", "left"), default="right")
    parser.add_argument("--repeat", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)
    try:
        origin = validate_url(args.url)
        if not 0 < args.timeout <= 60:
            raise RehearsalError("timeout must be >0 and <=60 seconds")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation prevents overwriting previous evidence.
        with args.output.open("x", encoding="utf-8") as stream:
            def write(record):
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                stream.flush()
            write({"kind": "rehearsal_start", "source": "mock", "execution_mode": "mock_llm",
                   "physical_validation_performed": False, "zone": args.zone, "repeat": args.repeat})
            try:
                with httpx.Client(base_url=origin, timeout=args.timeout, follow_redirects=False, trust_env=False) as client:
                    report = run_rehearsal(client, zone=args.zone, repeat=args.repeat,
                                           on_case=lambda case: write({"kind": "case", **case}))
                write({"kind": "rehearsal_end", **{key: value for key, value in report.items() if key not in ("cases", "kind")}})
            except (RehearsalError, httpx.HTTPError, ValueError) as error:
                write({"kind": "rehearsal_end", "status": "blocked", "error": type(error).__name__})
                print("시연을 시작/확인하지 못했습니다. 로컬 mock_llm 설정과 실행 상태를 확인하세요.")
                return 2
        print(f"Mock 시연: {report['status']}, {len(report['cases'])}건. 실물·실제 LLM 검증 아님.")
        return 0 if report["status"] == "passed" else 1
    except (OSError, ValueError):
        print("주소·시간 제한·출력 경로를 확인하세요. 기존 기록은 덮어쓰지 않습니다.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
