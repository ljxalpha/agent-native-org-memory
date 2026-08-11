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

# Give one leaf a brand new memory for this cycle. Every cycle must produce
# genuinely new work, otherwise "the health file changed" would be untestable.
write_cycle_memory() {
  local node_id="$1" topic="$2" cycle="$3"
  local agent_root
  agent_root="$(leaf_agent_root "$node_id")"
  mkdir -p "$agent_root/memory/shared/$topic"
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
  test -f "$DISTILL_WORK/manifest.json"
  test -n "$(find "$DISTILL_WORK/team" -type f -name '*.md' -print -quit)"
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

index=0
for node_id in "${LEAF_IDS[@]}"; do
  write_cycle_memory "$node_id" "${LEAF_TOPICS[$index]}" "$CYCLE"
  run_report "$node_id"
  index=$((index + 1))
done

run_distill
SHA="$(master_sha)"
for node_id in "${LEAF_IDS[@]}"; do
  run_subscribe "$node_id" "$SHA"
done

echo "local_repo_three_leaves.sh cycle $CYCLE passed"
