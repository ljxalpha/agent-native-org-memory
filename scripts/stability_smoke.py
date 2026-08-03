#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run repeated local integration smoke checks."
    )
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    parser.add_argument(
        "--script", type=Path, default=Path("tests/integration/local_repo.sh")
    )
    parser.add_argument("--output", type=Path, default=Path("stability_report.json"))
    args = parser.parse_args()
    results = []
    started = time.time()
    for index in range(args.iterations):
        run_started = time.time()
        proc = subprocess.run(
            ["bash", str(args.script)],
            text=True,
            capture_output=True,
            check=False,
        )
        results.append(
            {
                "iteration": index + 1,
                "returncode": proc.returncode,
                "duration_seconds": round(time.time() - run_started, 3),
                "stdout_tail": proc.stdout[-2000:],
                "stderr_tail": proc.stderr[-2000:],
            }
        )
        if proc.returncode != 0:
            break
        if args.sleep_seconds > 0 and index + 1 < args.iterations:
            time.sleep(args.sleep_seconds)
    summary = {
        "schema_version": "memory-net/stability-smoke/v1",
        "iterations_requested": args.iterations,
        "iterations_completed": len(results),
        "passed": all(item["returncode"] == 0 for item in results)
        and len(results) == args.iterations,
        "duration_seconds": round(time.time() - started, 3),
        "results": results,
    }
    args.output.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                k: summary[k]
                for k in ["passed", "iterations_completed", "duration_seconds"]
            },
            sort_keys=True,
        )
    )
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
