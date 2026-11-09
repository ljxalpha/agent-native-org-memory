#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory_net.distiller.metrics import (
    citation_resolution_rate,
    parse_failure_rate,
)

# Integer funnel counters summed across runs. The two global tuning rates are
# recomputed from these summed numerator/denominator pairs, never averaged from
# per-run rates (which would weight a 2-cluster run the same as a 200-cluster one).
_INT_TOTAL_KEYS = (
    "input_tokens",
    "output_tokens",
    "clusters_attempted",
    "parse_failures",
    "citation_failures",
    "citations_emitted",
    "citations_resolved",
    "fallback_selected",
    "fallback_accepted",
    "evidence_rule_rejections",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate distill metrics by run id.")
    parser.add_argument("repo", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = aggregate(args.repo)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


def aggregate(repo: Path) -> dict[str, Any]:
    runs = []
    totals: dict[str, float] = {key: 0 for key in _INT_TOTAL_KEYS}
    totals["estimated_cost_usd"] = 0.0
    totals["pre_publish_duration_seconds"] = 0.0
    category_distribution: dict[str, int] = {}
    for path in sorted((repo / "distill_runs").glob("*/metrics.json")):
        if path.parent.name == "latest":
            continue
        metrics = json.loads(path.read_text(encoding="utf-8"))
        runs.append(metrics)
        for key in _INT_TOTAL_KEYS:
            totals[key] += int(metrics.get(key, 0))
        totals["estimated_cost_usd"] += metrics.get("estimated_cost_usd", 0)
        totals["pre_publish_duration_seconds"] += metrics.get(
            "pre_publish_duration_seconds", metrics.get("duration_seconds", 0)
        )
        run_distribution = metrics.get("category_distribution") or {}
        if isinstance(run_distribution, dict):
            for category, count in run_distribution.items():
                category_distribution[str(category)] = category_distribution.get(
                    str(category), 0
                ) + int(count)
    return {
        "schema_version": "memory-net/metrics-aggregate/v1",
        "runs": runs,
        "totals": totals,
        "rates": {
            "parse_failure_rate": parse_failure_rate(
                int(totals["parse_failures"]), int(totals["clusters_attempted"])
            ),
            "citation_resolution_rate": citation_resolution_rate(
                int(totals["citations_resolved"]), int(totals["citations_emitted"])
            ),
        },
        "category_distribution": dict(sorted(category_distribution.items())),
        "run_count": len(runs),
    }


if __name__ == "__main__":
    raise SystemExit(main())
