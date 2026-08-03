#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())
    return proc.stdout


def main() -> int:
    repo = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    git(repo, "fetch", "--all", "--prune", check=False)
    branches = git(repo, "branch", "-r").splitlines()
    print("node_id\tstatus\tpublished_at\tlast_report_at\tlast_pull_at\trecent_errors")
    for raw in branches:
        branch = raw.strip()
        if not branch.startswith("origin/leaf/"):
            continue
        text = git(repo, "show", f"{branch}:.health.json", check=False)
        if not text.strip():
            continue
        health = json.loads(text)
        errors = ",".join(str(item) for item in health.get("recent_errors", []))
        print(
            "\t".join(
                [
                    str(health.get("node_id", branch.removeprefix("origin/leaf/"))),
                    str(health.get("status", "unknown")),
                    str(health.get("published_at", "")),
                    str(health.get("last_report_at", "")),
                    str(health.get("last_pull_at", "")),
                    errors,
                ]
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
