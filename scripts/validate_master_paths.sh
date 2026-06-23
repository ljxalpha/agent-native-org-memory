#!/usr/bin/env bash
set -euo pipefail

BASE_REF="${1:-HEAD~1}"

if ! git rev-parse --verify "$BASE_REF" >/dev/null 2>&1; then
  echo "[ABORT] cannot resolve base ref: $BASE_REF" >&2
  exit 2
fi

python3 - <<'PY' "$BASE_REF"
import subprocess
import sys

from memory_net.shared.util.paths import (
    MASTER_ALLOWED_EXACT_PATHS,
    MASTER_ALLOWED_PREFIXES,
)

base_ref = sys.argv[1]
result = subprocess.run(
    ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
    text=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
)
if result.returncode != 0:
    print(result.stderr, file=sys.stderr, end="")
    raise SystemExit(result.returncode)

for path in result.stdout.splitlines():
    if not path:
        continue
    if path in MASTER_ALLOWED_EXACT_PATHS:
        continue
    if any(path.startswith(f"{prefix}/") for prefix in MASTER_ALLOWED_PREFIXES):
        continue
    print(f"[ABORT] master/config writer path is not allowed: {path}", file=sys.stderr)
    raise SystemExit(3)
PY
