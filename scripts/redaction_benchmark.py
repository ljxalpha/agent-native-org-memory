#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory_net.shared.redaction import Redactor


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate redaction rules against JSONL samples."
    )
    parser.add_argument(
        "--config", type=Path, default=Path("config/redaction_patterns.yaml")
    )
    parser.add_argument(
        "--cases", type=Path, default=Path("tests/fixtures/redaction/benchmark.jsonl")
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate(args.config, args.cases)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0 if report["summary"]["failed"] == 0 else 1


def evaluate(config: Path, cases: Path) -> dict[str, Any]:
    redactor = Redactor.from_yaml(config)
    rows = []
    passed = 0
    failed = 0
    for line in cases.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        expected = set(str(item) for item in case.get("expected") or [])
        result = redactor.redact(str(case["text"]))
        actual = set(result.stats)
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        ok = not missing and not unexpected
        passed += int(ok)
        failed += int(not ok)
        rows.append(
            {
                "id": str(case.get("id") or ""),
                "passed": ok,
                "expected": sorted(expected),
                "actual": sorted(actual),
                "missing": missing,
                "unexpected": unexpected,
                "blocked": result.blocked,
            }
        )
    return {
        "schema_version": "memory-net/redaction-benchmark/v1",
        "config": str(config),
        "cases": str(cases),
        "summary": {"passed": passed, "failed": failed, "total": passed + failed},
        "cases_detail": rows,
    }


if __name__ == "__main__":
    raise SystemExit(main())
