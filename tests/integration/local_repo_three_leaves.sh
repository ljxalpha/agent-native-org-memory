#!/usr/bin/env bash
# One soak cycle over three mutually isolated synthetic leaves.
#
# tests/integration/local_repo.sh is a straight line: it builds a world, runs
# report -> distill -> subscribe once, and exits. That proves the flow works
# once; it cannot say anything about what holds after many rounds. This script
# is the same flow reshaped into a *repeatable* round so a driver
# (scripts/stability_soak.py) can call it N times against one accumulating
# workspace and assert cross-cycle properties on every leaf's .health.json.
#
# The three leaves are independent, not one state copied three times: each owns
# its AGENT_ROOT, its clone of the memory repo, and its MEMORY_NET_HOME (with
# its own install_id, local.yaml and config generation). That is what makes the
# three .health.json files three separate observations.
#
# Usage:
#   local_repo_three_leaves.sh --workspace <dir> [--cycle <n>]
#
# The first call bootstraps the workspace; later calls reuse it. Without
# --workspace a throwaway directory is created and removed on exit, which is
# only useful for a single-cycle smoke run.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORKSPACE=""
CYCLE=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --workspace) WORKSPACE="$2"; shift 2 ;;
    --cycle) CYCLE="$2"; shift 2 ;;
    -h|--help) sed -n '1,22p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$WORKSPACE" ]]; then
  WORKSPACE="$(mktemp -d "${TMPDIR:-/tmp}/memory-net-soak.XXXXXX")"
  trap 'rm -rf "$WORKSPACE"' EXIT
fi
mkdir -p "$WORKSPACE"
WORKSPACE="$(cd "$WORKSPACE" && pwd)"

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

# A neutral identity for every commit made below (the runner may have none), for
# this script's commits and for the distiller subprocess alike.
export GIT_AUTHOR_NAME="memory-net" GIT_AUTHOR_EMAIL="memory-net@example.invalid"
export GIT_COMMITTER_NAME="memory-net" GIT_COMMITTER_EMAIL="memory-net@example.invalid"

MEMORY_REPO="$WORKSPACE/team-memory.git"
DISTILL_WORK="$WORKSPACE/distill-work"

# node id -> topic directory under the agent root. Distinct topics keep the
# three leaves from reporting near-identical records.
LEAF_IDS=(alice-claude-01 bob-claude-01 carol-claude-01)
LEAF_TOPICS=(auth search release)

leaf_home() { echo "$WORKSPACE/home-$1"; }
leaf_work() { echo "$WORKSPACE/leaf-work-$1"; }
leaf_agent_root() { echo "$WORKSPACE/agent-$1"; }

master_sha() { git --git-dir="$MEMORY_REPO" rev-parse master; }

# Materialize the active config generation for one leaf out of `source_root`,
# stamping the master snapshot it came from, and repoint `config-current` at it
# atomically. This is what supplies `config_source_master_sha` to the leaf's
# operational identity, so the strict health check has something to validate.
activate_generation() {
  local home="$1" source_root="$2" sha="$3"
  python3 - "$home" "$source_root" "$sha" <<'PY'
import os
import sys
from pathlib import Path

from memory_net.shared.config.generation import materialize_generation

home, source_root, sha = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
generation = materialize_generation(
    source_root,
    home / "config.generations",
    adapter_name="claude-code",
    source_master_sha=sha,
)
current = home / "config-current"
tmp = current.with_name(".config-current.tmp")
if tmp.exists() or tmp.is_symlink():
    tmp.unlink()
os.symlink(generation, tmp)
os.replace(tmp, current)
PY
}

setup_workspace() {
  "$ROOT/scripts/bootstrap_repo.sh" --repo "$MEMORY_REPO" --source "$ROOT"
  local sha
  sha="$(master_sha)"

  local index=0
  for node_id in "${LEAF_IDS[@]}"; do
    local topic="${LEAF_TOPICS[$index]}"
    local home work agent_root
    home="$(leaf_home "$node_id")"
    work="$(leaf_work "$node_id")"
    agent_root="$(leaf_agent_root "$node_id")"

    mkdir -p "$agent_root/memory/shared/$topic" "$home/state"
    git clone --quiet "$MEMORY_REPO" "$work"
    git -C "$work" config user.email "memory-net@example.invalid"
    git -C "$work" config user.name "memory-net"

    python3 -c 'import uuid, sys; sys.stdout.write(uuid.uuid4().hex)' \
      > "$home/state/install_id"
    cat > "$home/local.yaml" <<YAML
node_id: "$node_id"
agent: claude-code
package_ref: "$ROOT"
YAML
    activate_generation "$home" "$ROOT" "$sha"
    index=$((index + 1))
  done

  python3 - "$WORKSPACE" "${LEAF_IDS[@]}" <<'PY'
import json
import sys
from pathlib import Path

workspace = Path(sys.argv[1])
leaves = [
    {
        "node_id": node_id,
        "health_path": str(workspace / f"leaf-work-{node_id}" / ".health.json"),
    }
    for node_id in sys.argv[2:]
]
(workspace / "leaves.json").write_text(
    json.dumps({"leaves": leaves}, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY
}

# How many cycles of memories a leaf keeps. The window has to be bounded: the
# 24h lane runs thousands of cycles, and an ever-growing corpus would make each
# distillation slower than the last, so the run's own cost -- not the system
# under test -- would decide when it fell over.
MEMORY_WINDOW_CYCLES=3

# How many distillation run directories master keeps. Every cycle commits a new
# `distill_runs/<run_id>/` into master, so without pruning the published tree --
# and every clone that fetches it -- grows with the run, for the same reason the
# memory window exists.
RUN_HISTORY_CYCLES=2

# Give one leaf a brand new memory for this cycle and retire the one that fell
# out of the window. Every cycle must produce genuinely new work, otherwise
# "the health file changed" would be untestable.
write_cycle_memory() {
  local node_id="$1" topic="$2" cycle="$3"
  local agent_root
  agent_root="$(leaf_agent_root "$node_id")"
  mkdir -p "$agent_root/memory/shared/$topic"
  local retired=$((cycle - MEMORY_WINDOW_CYCLES))
  if [[ "$retired" -ge 1 ]]; then
    rm -f "$agent_root/memory/shared/$topic/cycle-$retired.md"
  fi
  cat > "$agent_root/memory/shared/$topic/cycle-$cycle.md" <<MD
---
title: $topic practice, round $cycle
tags: [$topic, soak]
---
Round $cycle practice for $topic: keep the retry budget bounded and record the
outcome before moving on. Contact $topic-owner@example.com for context.
MD
}

run_report() {
  local node_id="$1"
  local home work agent_root
  home="$(leaf_home "$node_id")"
  work="$(leaf_work "$node_id")"
  agent_root="$(leaf_agent_root "$node_id")"
  MEMORY_NET_HOME="$home" python3 -m memory_net.leaf.cli report \
    --repo "$work" \
    --node-id "$node_id" \
    --agent-root "$agent_root" \
    --adapter "$ROOT/config/adapters/claude-code.yaml" \
    --redaction "$ROOT/config/redaction_patterns.yaml" > /dev/null
}

run_distill() {
  rm -rf "$DISTILL_WORK"
  git clone --quiet "$MEMORY_REPO" "$DISTILL_WORK"
  git -C "$DISTILL_WORK" config user.email "memory-net@example.invalid"
  git -C "$DISTILL_WORK" config user.name "memory-net"
  local distill_json run_id
  distill_json="$(python3 -m memory_net.distiller.cli run \
    --repo "$DISTILL_WORK" \
    --provider mock)"
  run_id="$(python3 - <<'PY' "$distill_json"
import json
import sys

print(json.loads(sys.argv[1])["run_id"])
PY
)"
  # The shared-pilot default publishes to a review branch; this local harness
  # stands in for the human approval by fast-forwarding master onto it.
  git -C "$DISTILL_WORK" push --quiet origin "release/$run_id:master"
  git -C "$DISTILL_WORK" fetch --quiet origin master
  git -C "$DISTILL_WORK" switch --quiet -C master origin/master
  # Retire the review branch once master carries its content: over a 24h run the
  # remote would otherwise accumulate one branch per cycle.
  git -C "$DISTILL_WORK" push --quiet origin --delete "release/$run_id" || true
  prune_distill_runs
  test -f "$DISTILL_WORK/manifest.json"
  test -n "$(find "$DISTILL_WORK/team" -type f -name '*.md' -print -quit)"
}

# Drop the run directories that fell out of the history window and publish the
# smaller tree. `latest` is left alone: it is the pointer the regressions read.
prune_distill_runs() {
  local stale
  stale="$(python3 - "$DISTILL_WORK" "$RUN_HISTORY_CYCLES" <<'PY'
import sys
from pathlib import Path

repo, keep = Path(sys.argv[1]), int(sys.argv[2])
runs = sorted(
    path
    for path in (repo / "distill_runs").glob("*")
    if path.is_dir() and path.name != "latest"
)
for path in runs[: max(0, len(runs) - keep)]:
    print(path.relative_to(repo).as_posix())
PY
)"
  [[ -n "$stale" ]] || return 0
  echo "$stale" | xargs -r git -C "$DISTILL_WORK" rm -rq --
  git -C "$DISTILL_WORK" commit -qm "prune distill run history"
  git -C "$DISTILL_WORK" push --quiet origin master
}

run_subscribe() {
  local node_id="$1" sha="$2"
  local home agent_root
  home="$(leaf_home "$node_id")"
  agent_root="$(leaf_agent_root "$node_id")"
  # Config follows master, exactly as a real leaf's would, so the generation is
  # re-materialized against the snapshot that was just published.
  activate_generation "$home" "$DISTILL_WORK" "$sha"
  MEMORY_NET_HOME="$home" python3 -m memory_net.leaf.cli subscribe \
    --repo "$DISTILL_WORK" \
    --agent-root "$agent_root" \
    --adapter "$ROOT/config/adapters/claude-code.yaml" \
    --config-revision "cycle-$CYCLE" \
    --state-dir "$home/state" > /dev/null
  test -f "$agent_root/memory/team/INDEX.md"
  grep -q "Team Memory Index" "$agent_root/memory/team/INDEX.md"
}

if [[ ! -f "$WORKSPACE/leaves.json" ]]; then
  setup_workspace
fi

# Fault injection for the soak's own regression: the named leaf reports on the
# first cycle and then stops, while the other two carry on. That is the shape of
# the failure the driver's "the health payload must change" check exists for, and
# it is the only way to exercise that check against the real pipeline.
STALL_LEAF="${MEMORY_NET_SOAK_STALL_LEAF:-}"

index=0
for node_id in "${LEAF_IDS[@]}"; do
  index_now=$index
  index=$((index + 1))
  if [[ "$node_id" == "$STALL_LEAF" && "$CYCLE" -gt 1 ]]; then
    continue
  fi
  write_cycle_memory "$node_id" "${LEAF_TOPICS[$index_now]}" "$CYCLE"
  run_report "$node_id"
done

run_distill
SHA="$(master_sha)"
for node_id in "${LEAF_IDS[@]}"; do
  run_subscribe "$node_id" "$SHA"
done

echo "local_repo_three_leaves.sh cycle $CYCLE passed"
