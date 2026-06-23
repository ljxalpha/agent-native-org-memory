#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory_net.distiller.golden_eval import evaluate_golden_fixture


def main() -> int:
    parser = argparse.ArgumentParser(description="Run golden cluster evaluation.")
    parser.add_argument("fixture", type=Path)
    args = parser.parse_args()
    report = evaluate(args.fixture)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


def evaluate(fixture: Path) -> dict[str, Any]:
    return evaluate_golden_fixture(fixture)


if __name__ == "__main__":
    raise SystemExit(main())
