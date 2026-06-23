#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMPROOT="$(mktemp -d "${TMPDIR:-/tmp}/memory-net-e2e.XXXXXX")"
trap 'rm -rf "$TMPROOT"' EXIT

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

# Make every git commit in this self-contained regression independent of any
# ambient git identity (CI runners have none configured). Setting author/committer
# via env covers both this script's commits and those made by the distiller
# subprocess, which otherwise fails with "Author identity unknown".
export GIT_AUTHOR_NAME="memory-net" GIT_AUTHOR_EMAIL="memory-net@example.invalid"
export GIT_COMMITTER_NAME="memory-net" GIT_COMMITTER_EMAIL="memory-net@example.invalid"

MEMORY_REPO="$TMPROOT/team-memory.git"
LEAF_WORK_A="$TMPROOT/leaf-work-a"
LEAF_WORK_B="$TMPROOT/leaf-work-b"
DISTILL_WORK="$TMPROOT/distill-work"
AGENT_ROOT_A="$TMPROOT/agent-a"
AGENT_ROOT_B="$TMPROOT/agent-b"
export MEMORY_NET_HOME="$TMPROOT/home"

mkdir -p "$AGENT_ROOT_A/memory/shared/auth"
cat > "$AGENT_ROOT_A/memory/shared/auth/oauth.md" <<'MD'
---
title: OAuth retry practice
tags: [auth, retry]
---
When OAuth token refresh returns 429, retry with bounded exponential backoff.
Contact alice@example.com for historical context.
MD

mkdir -p "$AGENT_ROOT_B/memory/shared/search"
cat > "$AGENT_ROOT_B/memory/shared/search/indexing.md" <<'MD'
---
title: Search indexing practice
tags: [search]
---
Rebuild the search index after schema changes.
Contact bob@example.com for rollout notes.
MD

"$ROOT/scripts/bootstrap_repo.sh" --repo "$MEMORY_REPO" --source "$ROOT"

git clone "$MEMORY_REPO" "$LEAF_WORK_A"
git -C "$LEAF_WORK_A" config user.email "memory-net@example.invalid"
git -C "$LEAF_WORK_A" config user.name "memory-net"

python3 -m memory_net.leaf.cli report \
  --repo "$LEAF_WORK_A" \
  --node-id alice-claude-01 \
  --agent-root "$AGENT_ROOT_A" \
  --adapter "$ROOT/config/adapters/claude-code.yaml" \
  --redaction "$ROOT/config/redaction_patterns.yaml"

ALICE_REPORTED="$(find "$LEAF_WORK_A/memories" -type f -name '*.md' -print -quit)"
grep -q '<REDACTED:email>' "$ALICE_REPORTED"
! grep -q 'alice@example.com' "$ALICE_REPORTED"

git clone "$MEMORY_REPO" "$LEAF_WORK_B"
git -C "$LEAF_WORK_B" config user.email "memory-net@example.invalid"
git -C "$LEAF_WORK_B" config user.name "memory-net"

python3 -m memory_net.leaf.cli report \
  --repo "$LEAF_WORK_B" \
  --node-id bob-claude-01 \
  --agent-root "$AGENT_ROOT_B" \
  --adapter "$ROOT/config/adapters/claude-code.yaml" \
  --redaction "$ROOT/config/redaction_patterns.yaml"

BOB_REPORTED="$(find "$LEAF_WORK_B/memories" -type f -name '*.md' -print -quit)"
grep -q '<REDACTED:email>' "$BOB_REPORTED"
! grep -q 'bob@example.com' "$BOB_REPORTED"

git clone "$MEMORY_REPO" "$DISTILL_WORK"
DISTILL_JSON="$(python3 -m memory_net.distiller.cli run \
  --repo "$DISTILL_WORK" \
  --provider mock)"
echo "$DISTILL_JSON"
RUN_ID="$(python3 - <<'PY' "$DISTILL_JSON"
import json
import sys

print(json.loads(sys.argv[1])["run_id"])
PY
)"

# The default shared-pilot path publishes to a review branch. Simulate approval
# in this local regression by fast-forwarding master to the release branch.
git -C "$DISTILL_WORK" push origin "release/$RUN_ID:master"
git -C "$DISTILL_WORK" fetch origin master
git -C "$DISTILL_WORK" switch -C master origin/master

test -f "$DISTILL_WORK/manifest.json"
test -f "$DISTILL_WORK/.health.json"
test -f "$DISTILL_WORK/distill_runs/latest/metrics.json"
test -n "$(find "$DISTILL_WORK/team" -type f -name '*.md' -print -quit)"

python3 - <<'PY' "$DISTILL_WORK"
import json
import sys
from pathlib import Path

from memory_net.shared.schema import validate_health, validate_master_manifest

root = Path(sys.argv[1])
validate_master_manifest(json.loads((root / "manifest.json").read_text()))
validate_health(json.loads((root / ".health.json").read_text()))
manifest = json.loads((root / "manifest.json").read_text())
assert manifest["subject_index"], manifest
assert manifest["citation_backrefs"], manifest
metrics = json.loads((root / "distill_runs/latest/metrics.json").read_text())
assert metrics["input_records"] == 2, metrics
assert metrics["products"] >= 1, metrics
assert metrics["skipped_clusters"] == 0, metrics
PY

python3 - <<'PY' "$DISTILL_WORK"
import json
import subprocess
import sys
from pathlib import Path

from memory_net.shared.util.frontmatter import split_frontmatter

root = Path(sys.argv[1])
manifest = json.loads((root / "manifest.json").read_text())
snapshot = json.loads(
    (root / "distill_runs" / manifest["last_run_id"] / "snapshot.json").read_text()
)
assert snapshot["leaf_refs"], snapshot
for product in manifest["products_active"].values():
    content = (root / product["relative_path"]).read_text(encoding="utf-8")
    frontmatter, _body = split_frontmatter(content)
    for citation in frontmatter["citations"]:
        source = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "show",
                f"{citation['source_branch_sha']}:{citation['relative_path']}",
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout
        cited_frontmatter, _ = split_frontmatter(source)
        assert cited_frontmatter["revision_hash"] == citation["revision_hash"]
PY

python3 - <<'PY' "$DISTILL_WORK" "$MEMORY_NET_HOME"
import os
import shutil
import sys
from pathlib import Path

from memory_net.shared.config.generation import materialize_generation

repo = Path(sys.argv[1])
home = Path(sys.argv[2])
generation = materialize_generation(
    repo,
    home / "config.generations",
    adapter_name="claude-code",
)
current = home / "config-current"
tmp = current.with_name(".config-current.tmp")
if tmp.exists() or tmp.is_symlink():
    tmp.unlink()
os.symlink(generation, tmp)
os.replace(tmp, current)
PY

python3 -m memory_net.leaf.cli subscribe \
  --repo "$DISTILL_WORK" \
  --agent-root "$AGENT_ROOT_A" \
  --adapter "$ROOT/config/adapters/claude-code.yaml" \
  --config-revision test \
  --state-dir "$MEMORY_NET_HOME/state"

test -f "$AGENT_ROOT_A/memory/team/INDEX.md"
test -n "$(find "$AGENT_ROOT_A/memory/team" -type f -name '*.md' ! -name 'INDEX.md' -print -quit)"
test -f "$MEMORY_NET_HOME/state/last_pull.json"

python3 -m memory_net.leaf.cli subscribe \
  --repo "$DISTILL_WORK" \
  --agent-root "$AGENT_ROOT_B" \
  --adapter "$ROOT/config/adapters/claude-code.yaml" \
  --config-revision test \
  --state-dir "$MEMORY_NET_HOME/state-b"

test -f "$AGENT_ROOT_B/memory/team/INDEX.md"
grep -q "Team Memory Index" "$AGENT_ROOT_A/memory/team/INDEX.md"
grep -q "Team Memory Index" "$AGENT_ROOT_B/memory/team/INDEX.md"

echo "local_repo.sh passed"
