#!/usr/bin/env bash
# Hermetic regression for scripts/install_leaf.sh.
#
# The installer used to be untestable: it unconditionally rewrote the invoking
# user's crontab and started a long-lived daemon, so running it once polluted
# the developer machine or the CI runner. With --no-cron / --no-daemon it can be
# exercised end to end inside a temporary directory, against a local bare remote
# and a local package path -- no network, no ambient git identity, no daemon.
#
# This script asserts the crontab is untouched. That assertion is not a nicety:
# it is the precondition that makes running this regression on a real machine
# acceptable, so it runs even when `crontab` is unavailable.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMPROOT="$(mktemp -d "${TMPDIR:-/tmp}/memory-net-install.XXXXXX")"
cleanup() {
  # Kill before removing the tree: a daemon left running against a deleted home
  # would be exactly the machine pollution this harness exists to prevent.
  if [[ -f "$TMPROOT/home-daemon/state/daemon.pid" ]]; then
    kill "$(cat "$TMPROOT/home-daemon/state/daemon.pid")" 2>/dev/null || true
  fi
  rm -rf "$TMPROOT"
}
trap cleanup EXIT

# Neutral identity for every commit made below, so the regression does not
# depend on (or read) the machine's git configuration.
export GIT_AUTHOR_NAME="memory-net" GIT_AUTHOR_EMAIL="memory-net@example.invalid"
export GIT_COMMITTER_NAME="memory-net" GIT_COMMITTER_EMAIL="memory-net@example.invalid"

MEMORY_REPO="$TMPROOT/team-memory.git"
MN_HOME="$TMPROOT/home"
AGENT_ROOT="$TMPROOT/agent"
NODE_ID="alice-claudecode-01"

mkdir -p "$AGENT_ROOT/memory/shared"
cat > "$AGENT_ROOT/memory/shared/note.md" <<'MD'
---
title: Placeholder practice
---
A shared note so the leaf has something to scan.
MD

"$ROOT/scripts/bootstrap_repo.sh" --repo "$MEMORY_REPO" --source "$ROOT"

# Snapshot the user's crontab before the installer runs. `crontab -l` exits
# non-zero when no crontab exists; that is a valid "before" state too.
CRON_BEFORE="$TMPROOT/cron-before"
CRON_AFTER="$TMPROOT/cron-after"
crontab -l > "$CRON_BEFORE" 2>/dev/null || : > "$CRON_BEFORE"

# --package-ref is a local path, so pip installs from the working tree instead
# of reaching the network. The 40-char-sha rule only applies to git+* refs.
MEMORY_NET_HOME="$MN_HOME" "$ROOT/scripts/install_leaf.sh" \
  --repo "$MEMORY_REPO" \
  --agent claude-code \
  --node-id "$NODE_ID" \
  --agent-root "$AGENT_ROOT" \
  --package-ref "$ROOT" \
  --home "$MN_HOME" \
  --no-cron \
  --no-daemon

crontab -l > "$CRON_AFTER" 2>/dev/null || : > "$CRON_AFTER"
diff -u "$CRON_BEFORE" "$CRON_AFTER" \
  || { echo "[FAIL] the installer modified the user crontab" >&2; exit 1; }
! grep -q "memory-net:leaf-daemon" "$CRON_AFTER" \
  || { echo "[FAIL] a memory-net cron entry was registered" >&2; exit 1; }

test ! -f "$MN_HOME/state/daemon.pid" \
  || { echo "[FAIL] --no-daemon still started a daemon" >&2; exit 1; }

# Installed layout.
test -s "$MN_HOME/state/install_id"
test -f "$MN_HOME/local.yaml"
test -L "$MN_HOME/config-current"
test -d "$MN_HOME/config-current/adapters"
test -f "$MN_HOME/config-current/config_revision"
test -f "$MN_HOME/config-current/config_source_master_sha"
test -x "$MN_HOME/venv/bin/python"

grep -q "node_id: \"$NODE_ID\"" "$MN_HOME/local.yaml"
grep -q "package_ref: \"$ROOT\"" "$MN_HOME/local.yaml"

# Exporting master config must not stage anything on the leaf branch. A dirty
# worktree here is not cosmetic: the daemon refuses to switch branches on one.
DIRTY="$(git -C "$MN_HOME/repo" status --porcelain)"
test -z "$DIRTY" \
  || { echo "[FAIL] the install left the leaf worktree dirty:" >&2; \
       echo "$DIRTY" >&2; exit 1; }

# The leaf branch and its ownership triple must be on the remote, not just local.
git --git-dir="$MEMORY_REPO" show-ref --verify --quiet "refs/heads/leaf/$NODE_ID"
git --git-dir="$MEMORY_REPO" show "leaf/$NODE_ID:.owner.json" > "$TMPROOT/owner.json"

INSTALL_ID="$(cat "$MN_HOME/state/install_id")"
python3 - "$TMPROOT/owner.json" "$NODE_ID" "$INSTALL_ID" <<'PY'
import json
import sys

owner = json.loads(open(sys.argv[1], encoding="utf-8").read())
node_id, install_id = sys.argv[2], sys.argv[3]

assert owner["node_id"] == node_id, owner
assert owner["install_id"] == install_id, owner
for field in ("owner_fingerprint", "host"):
    assert owner.get(field), f"{field} is missing from the ownership triple"
PY

python3 - "$MN_HOME/state/build_info.json" "$ROOT" <<'PY'
import json
import sys

build_info = json.loads(open(sys.argv[1], encoding="utf-8").read())
# A local-path ref pins no commit, so the build id must degrade *visibly*.
assert build_info["package_ref"] == sys.argv[2], build_info
assert build_info["build_id_source"] == "code_version", build_info
assert build_info["build_id"] == build_info["code_version"], build_info
assert len(build_info["master_sha"]) == 40, build_info
assert build_info["installed_at"].endswith("Z"), build_info
PY

python3 - "$MN_HOME/state/install_state.json" <<'PY'
import json
import sys

state = json.loads(open(sys.argv[1], encoding="utf-8").read())
# --no-daemon stops one step short of DAEMON_STARTED.
assert state["status"] == "HINTS_INSTALLED", state
PY

# --- Failure staging and dirty recovery -------------------------------------
# Break the remote so a *second* install fails after the venv but before the
# repo is usable, then assert the state machine says which stage broke and that
# the failure record leaks nothing about the environment.
BROKEN_HOME="$TMPROOT/home-broken"
mv "$MEMORY_REPO" "$MEMORY_REPO.moved"
set +e
MEMORY_NET_HOME="$BROKEN_HOME" "$ROOT/scripts/install_leaf.sh" \
  --repo "$MEMORY_REPO" \
  --agent claude-code \
  --node-id "$NODE_ID" \
  --agent-root "$AGENT_ROOT" \
  --package-ref "$ROOT" \
  --home "$BROKEN_HOME" \
  --no-cron \
  --no-daemon > "$TMPROOT/broken.out" 2> "$TMPROOT/broken.err"
BROKEN_EXIT=$?
set -e
mv "$MEMORY_REPO.moved" "$MEMORY_REPO"
test "$BROKEN_EXIT" -ne 0 \
  || { echo "[FAIL] a broken remote did not fail the install" >&2; exit 1; }

python3 - "$BROKEN_HOME/state/install_state.json" <<'PY'
import json
import re
import sys

raw = open(sys.argv[1], encoding="utf-8").read()
state = json.loads(raw)
assert state["status"].startswith("FAILED:"), state
assert state["status"] == "FAILED:VENV_READY", state
assert isinstance(state["exit_code"], int) and state["exit_code"] != 0, state
# The failure record is pasted into support issues: stage and code only.
assert set(state) == {"status", "exit_code", "at"}, state
forbidden = re.compile(r"https?://|git@|ssh://|/home/|/Users/|Traceback", re.I)
assert not forbidden.search(raw), raw
PY

# Re-running against the repaired remote must leave evidence of the dirty home
# and then succeed, without silently overwriting what was there.
MEMORY_NET_HOME="$BROKEN_HOME" "$ROOT/scripts/install_leaf.sh" \
  --repo "$MEMORY_REPO" \
  --agent claude-code \
  --node-id "$NODE_ID-b" \
  --agent-root "$AGENT_ROOT" \
  --package-ref "$ROOT" \
  --home "$BROKEN_HOME" \
  --no-cron \
  --no-daemon > "$TMPROOT/recover.out" 2> "$TMPROOT/recover.err"

python3 - "$BROKEN_HOME" <<'PY'
import json
import sys
from pathlib import Path

home = Path(sys.argv[1])
inventories = sorted(home.glob("state/dirty_recovery/*/inventory.json"))
assert inventories, "no dirty recovery evidence was recorded"
inventory = json.loads(inventories[0].read_text(encoding="utf-8"))
assert inventory["reasons"], inventory
assert inventory["entries"], inventory
# Evidence is an inventory, not a copy: no file contents anywhere in it.
raw = inventories[0].read_text(encoding="utf-8")
assert "node_id" not in raw, raw
state = json.loads((home / "state" / "install_state.json").read_text(encoding="utf-8"))
assert state["status"] == "HINTS_INSTALLED", state
PY

# The active generation must be hash-consistent with its own directory name.
# That is the property the shared materialization re-verifies after moving into
# place, and which the previous hand-rolled sequence never checked.
python3 - "$MN_HOME/config-current" <<'PY'
import sys
from pathlib import Path

from memory_net.shared.config.generation import canonical_generation_hash

generation = Path(sys.argv[1]).resolve()
assert canonical_generation_hash(generation) == generation.name, generation
assert (generation / "config_revision").read_text(encoding="utf-8") == generation.name
PY

# --- Post-install verification ----------------------------------------------
# Everything above ran with --no-daemon. Run one install *with* the daemon, in
# its own temporary home, to exercise the bounded post-install check: starting a
# process is not the same as installing a working leaf.
DAEMON_HOME="$TMPROOT/home-daemon"
MEMORY_NET_HOME="$DAEMON_HOME" "$ROOT/scripts/install_leaf.sh" \
  --repo "$MEMORY_REPO" \
  --agent claude-code \
  --node-id "$NODE_ID-d" \
  --agent-root "$AGENT_ROOT" \
  --package-ref "$ROOT" \
  --home "$DAEMON_HOME" \
  --no-cron > "$TMPROOT/daemon.out" 2> "$TMPROOT/daemon.err"

crontab -l > "$CRON_AFTER" 2>/dev/null || : > "$CRON_AFTER"
diff -u "$CRON_BEFORE" "$CRON_AFTER" \
  || { echo "[FAIL] the daemon install modified the user crontab" >&2; exit 1; }

python3 - "$DAEMON_HOME/state/install_state.json" <<'PY'
import json
import sys

state = json.loads(open(sys.argv[1], encoding="utf-8").read())
# DAEMON_STARTED is only written after post-install verification passes.
assert state["status"] == "DAEMON_STARTED", state
PY

# The verification artifact must itself pass the strict check, which is what
# proves the installed layout resolves to a complete operational identity.
MEMORY_NET_HOME="$DAEMON_HOME" "$DAEMON_HOME/venv/bin/python" \
  -m memory_net.leaf.cli status \
  --health "$DAEMON_HOME/state/post_install_health.json" >/dev/null

DAEMON_PID="$(cat "$DAEMON_HOME/state/daemon.pid")"
kill "$DAEMON_PID" 2>/dev/null || true
for _ in 1 2 3 4 5 6 7 8 9 10; do
  kill -0 "$DAEMON_PID" 2>/dev/null || break
  sleep 1
done
! kill -0 "$DAEMON_PID" 2>/dev/null \
  || { echo "[FAIL] the daemon survived cleanup" >&2; exit 1; }

echo "install_leaf_local.sh passed"
