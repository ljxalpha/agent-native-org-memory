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
trap 'rm -rf "$TMPROOT"' EXIT

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

echo "install_leaf_local.sh passed"
