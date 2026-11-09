#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.product_diff import diff_runs


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a readable diff for two prompt run outputs."
    )
    parser.add_argument("repo", type=Path)
    parser.add_argument("run_a")
    parser.add_argument("run_b")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    diff = diff_runs(args.repo, args.run_a, args.run_b)
    report = {
        "schema_version": "memory-net/prompt-ab/v1",
        "run_a": args.run_a,
        "run_b": args.run_b,
        "diff": diff,
    }
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
