#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render prompt A/B JSON diff as Markdown."
    )
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    data = json.loads(args.report.read_text(encoding="utf-8"))
    print(f"# Prompt A/B: {data['run_a']} vs {data['run_b']}\n")
    print("```diff")
    print(data.get("diff", ""))
    print("```")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
