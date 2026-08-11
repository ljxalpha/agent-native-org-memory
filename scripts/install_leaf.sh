#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage: install_leaf.sh --repo <memory-repo> --agent <agent> --node-id <id> --agent-root <path> --package-ref <ref>
       [--home <dir>] [--no-cron] [--no-daemon]

  --no-cron     do not register the leaf daemon in the user's crontab
  --no-daemon   do not start the leaf daemon

Both default to off, i.e. a production install still installs cron and starts
the daemon. They exist because those two steps are the only ones that write
outside $MEMORY_NET_HOME -- to the machine's own crontab and process table --
which is what otherwise makes this script impossible to regression-test.
Equivalent environment variables: MEMORY_NET_INSTALL_NO_CRON=1,
MEMORY_NET_INSTALL_NO_DAEMON=1 (a command-line flag wins over the variable).
USAGE
}

MEMORY_REPO=""
AGENT="claude-code"
NODE_ID=""
AGENT_ROOT=""
PACKAGE_REF=""
MN_HOME="${MEMORY_NET_HOME:-$HOME/.memory-net}"
NO_CRON="${MEMORY_NET_INSTALL_NO_CRON:-0}"
NO_DAEMON="${MEMORY_NET_INSTALL_NO_DAEMON:-0}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) MEMORY_REPO="$2"; shift 2;;
    --agent) AGENT="$2"; shift 2;;
    --node-id) NODE_ID="$2"; shift 2;;
    --agent-root) AGENT_ROOT="$2"; shift 2;;
    --package-ref) PACKAGE_REF="$2"; shift 2;;
    --home) MN_HOME="$2"; shift 2;;
    --no-cron) NO_CRON=1; shift;;
    --no-daemon) NO_DAEMON=1; shift;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown arg: $1" >&2; usage; exit 2;;
  esac
done

if [[ -z "$MEMORY_REPO" || -z "$NODE_ID" || -z "$AGENT_ROOT" || -z "$PACKAGE_REF" ]]; then
  usage; exit 2
fi

case "$NODE_ID" in
  *[!a-z0-9-]*|""|-*|*-)
    echo "[ABORT] node_id must contain lowercase letters, digits, and hyphens only" >&2; exit 2;;
esac

git check-ref-format --branch "leaf/$NODE_ID" >/dev/null

case "$PACKAGE_REF" in
  *@main|*@master|*@HEAD|*@main\#*|*@master\#*|*@HEAD\#*)
    echo "[ABORT] floating package refs are not allowed" >&2; exit 2;;
esac

python3 - <<'PY' "$PACKAGE_REF" "$MEMORY_REPO"
import sys
import urllib.parse

for raw in sys.argv[1:]:
    if "://" not in raw:
        continue
    parsed = urllib.parse.urlparse(raw)
    if parsed.username or parsed.password:
        print("[ABORT] embedded credentials are not allowed", file=sys.stderr)
        raise SystemExit(2)
PY

if [[ "$PACKAGE_REF" == git+* ]]; then
  REF="${PACKAGE_REF##*@}"
  [[ "$REF" =~ ^[0-9a-fA-F]{40}$ ]] || {
    echo "[ABORT] M1b accepts only 40-char commit sha package refs" >&2; exit 3;
  }
fi

mkdir -p "$MN_HOME/state" "$MN_HOME/config.generations"
python3 -m venv "$MN_HOME/venv"
PYTHON="$MN_HOME/venv/bin/python"
"$PYTHON" -m pip install --upgrade pip >/dev/null
"$PYTHON" -m pip install "$PACKAGE_REF" >/dev/null

# From here on every failure records which stage it happened in. Under
# `set -euo pipefail` the script used to die silently between its two state
# writes, leaving a file that described a previous run -- exactly when someone
# needs to know how far this one got.
STAGE="PREFLIGHT"
record_stage() {
  STAGE="$1"
  "$PYTHON" -m memory_net.leaf.install_state record \
    --state-dir "$MN_HOME/state" --status "$1"
}
abort() {
  # An explicit `exit` bypasses the ERR trap, which would leave the previous
  # (successful) state on disk describing a run that in fact failed.
  local code="$1"; shift
  echo "$@" >&2
  exit "$code"
}
record_failure() {
  local code=$?
  # Stage and exit code only. Command output here would carry the remote URL,
  # absolute paths and possibly a credential fragment into a file that gets
  # pasted into support issues.
  "$PYTHON" -m memory_net.leaf.install_state record \
    --state-dir "$MN_HOME/state" --status "FAILED:$STAGE" --exit-code "$code" \
    >/dev/null 2>&1 || true
  exit "$code"
}
trap record_failure ERR

# PREFLIGHT. Placed here rather than at the very top because the probe ships
# with the package, and because the interpreter that matters is the venv's --
# it is the one that will run the daemon. Everything still to come (clone,
# orphan-branch push, cron, daemon) is either expensive or hard to undo, so an
# environment that cannot support the install is refused before any of it.
"$PYTHON" -m memory_net.leaf.install_probe preflight --home "$MN_HOME"

# Dirty recovery: a previous run may have left the home inconsistent. Record an
# inventory (paths, sizes, hashes -- never contents) before proceeding, so a
# state this run is about to replace can still be reasoned about afterwards.
# Nothing is deleted or moved here.
"$PYTHON" -m memory_net.leaf.install_state recover --home "$MN_HOME"

record_stage VENV_READY
INSTALL_ID_FILE="$MN_HOME/state/install_id"
if [[ -f "$INSTALL_ID_FILE" ]]; then
  INSTALL_ID="$(cat "$INSTALL_ID_FILE")"
else
  INSTALL_ID="$(python3 - <<'PY'
import uuid
print("inst-" + uuid.uuid4().hex)
PY
)"
  printf "%s\n" "$INSTALL_ID" > "$INSTALL_ID_FILE"
fi
OWNER_FP="$(python3 - <<'PY'
import hashlib
import platform

raw = f"{platform.node()}:{platform.system()}:{platform.machine()}".encode()
print("ofp-" + hashlib.sha256(raw).hexdigest()[:12])
PY
)"

if [[ ! -d "$MN_HOME/repo/.git" ]]; then
  git clone "$MEMORY_REPO" "$MN_HOME/repo"
else
  cd "$MN_HOME/repo"
  EXISTING="$(git remote get-url origin)"
  [[ "$EXISTING" == "$MEMORY_REPO" ]] \
    || abort 4 "[ABORT] existing repo origin differs: $EXISTING"
fi

cd "$MN_HOME/repo"
git fetch origin
record_stage REPO_READY
git show origin/master:bootstrap_state.json >/dev/null
git show origin/master:manifest.json >/dev/null
ADAPTER_YAML="$(git show "origin/master:config/adapters/$AGENT.yaml")"

# SCHEMA_PROBE: existence is not compatibility. Validate the remote's schemas --
# including one representative team product -- before adopting them, so an
# incompatible or corrupted master is refused here instead of surfacing during
# the first sync, far from its cause.
STAGE="SCHEMA_PROBE"
PROBE_DIR="$(mktemp -d "$MN_HOME/.probe.XXXXXX")"
git show origin/master:bootstrap_state.json > "$PROBE_DIR/bootstrap_state.json"
git show origin/master:manifest.json > "$PROBE_DIR/manifest.json"
PROBE_ARGS=(
  --bootstrap-state "$PROBE_DIR/bootstrap_state.json"
  --manifest "$PROBE_DIR/manifest.json"
)
# Enumerate to a file so a git failure stays distinguishable from "the team
# has no products yet". Collapsing the two would let a broken remote skip the
# product check entirely and still pass the probe.
git ls-tree -r --name-only origin/master -- team > "$PROBE_DIR/team-listing"
SAMPLE_PRODUCT="$(grep -E '\.md$' "$PROBE_DIR/team-listing" | head -n 1 || true)"
if [[ -n "$SAMPLE_PRODUCT" ]]; then
  git show "origin/master:$SAMPLE_PRODUCT" > "$PROBE_DIR/product.md"
  PROBE_ARGS+=(--team-product "$PROBE_DIR/product.md")
fi
"$PYTHON" -m memory_net.leaf.install_probe schema \
  "${PROBE_ARGS[@]}" \
  --package-ref "$PACKAGE_REF"
rm -rf "$PROBE_DIR"
if grep -Eq '^support_status:[[:space:]]*experimental[[:space:]]*$' <<<"$ADAPTER_YAML"; then
  if [[ "${MEMORY_NET_ALLOW_EXPERIMENTAL_ADAPTER:-}" != "1" ]]; then
    abort 4 "[ABORT] adapter '$AGENT' is experimental; set MEMORY_NET_ALLOW_EXPERIMENTAL_ADAPTER=1 to opt in"
  fi
fi

if git show-ref --verify --quiet "refs/remotes/origin/leaf/$NODE_ID"; then
  OWNER_JSON="$(git show "origin/leaf/$NODE_ID:.owner.json")"
  REMOTE_INSTALL_ID="$(python3 - <<'PY' "$OWNER_JSON"
import json
import sys

owner = json.loads(sys.argv[1])
print(owner.get("install_id", ""))
PY
)"
  REMOTE_OWNER_FP="$(python3 - <<'PY' "$OWNER_JSON"
import json
import sys

owner = json.loads(sys.argv[1])
print(owner.get("owner_fingerprint", ""))
PY
)"
  REMOTE_HOST="$(python3 - <<'PY' "$OWNER_JSON"
import json
import sys

owner = json.loads(sys.argv[1])
print(owner.get("host", ""))
PY
)"
  if [[ "$REMOTE_INSTALL_ID" != "$INSTALL_ID" || "$REMOTE_OWNER_FP" != "$OWNER_FP" || "$REMOTE_HOST" != "$(hostname)" ]]; then
    abort 5 "[ABORT] leaf/$NODE_ID is owned by another install"
  fi
  git switch -C "leaf/$NODE_ID" "origin/leaf/$NODE_ID"
else
  git switch --orphan "leaf/$NODE_ID"
  git rm -rf . >/dev/null 2>&1 || true
  NOW="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  cat > .owner.json <<JSON
{"schema_version":"memory-net/owner/v1","node_id":"$NODE_ID","agent":"$AGENT","agent_root":"$AGENT_ROOT","install_id":"$INSTALL_ID","owner_fingerprint":"$OWNER_FP","host":"$(hostname)","created_at":"$NOW"}
JSON
  cat > .health.json <<JSON
{"schema_version":"memory-net/health/v1","node_id":"$NODE_ID","role":"leaf","status":"healthy","last_heartbeat_at":"$NOW","published_at":"$NOW","recent_errors":[],"redaction_incidents":{"blocked_reports":0,"scanner_hits":{}}}
JSON
  cat > manifest.json <<JSON
{"schema_version":"memory-net/leaf-manifest/v1","node_id":"$NODE_ID","entries":{},"tombstones":{}}
JSON
  printf "# leaf/%s\n" "$NODE_ID" > README.md
  printf ".tmp\n" > .gitignore
  git add .owner.json .health.json manifest.json README.md .gitignore
  git commit -m "initialize leaf $NODE_ID"
  git push origin "leaf/$NODE_ID"
fi

cat > "$MN_HOME/local.yaml" <<YAML
node_id: "$NODE_ID"
agent: "$AGENT"
agent_root: "$AGENT_ROOT"
memory_repo: "$MEMORY_REPO"
package_ref: "$PACKAGE_REF"
YAML

record_stage BRANCH_READY

TMP_GEN="$(mktemp -d "$MN_HOME/config.generations/.gen.XXXXXX")"
mkdir -p "$TMP_GEN"
# Snapshot provenance: which master commit this generation was exported from.
# It is recorded as a marker inside the generation, never as hashed content.
MASTER_SHA="$(git rev-parse origin/master)"
# What this install actually installed. local.yaml holds the ref but not the
# resolved build id nor the degradation that produced it, so without this there
# is no on-disk answer to "which build is this leaf running" after the script
# exits.
"$PYTHON" -m memory_net.leaf.install_probe build-info \
  --state-dir "$MN_HOME/state" \
  --package-ref "$PACKAGE_REF" \
  --master-sha "$MASTER_SHA" \
  --installed-at "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
# Export through a throwaway index. `git checkout -- <paths>` stages what it
# writes, and this repository is sitting on the leaf branch, so exporting master
# config this way left the leaf worktree permanently dirty -- which the daemon
# then refuses to switch branches on.
GIT_INDEX_FILE="$TMP_GEN/.export-index" \
  git --work-tree="$TMP_GEN" checkout origin/master -- bootstrap_state.json config prompts >/dev/null
rm -f "$TMP_GEN/.export-index"

# Materialize through the shared implementation rather than by hand. The
# previous inline mktemp -> hash -> mv -> ln sequence was a second, unprotected
# path to the same result: it skipped the post-move hash re-verification and the
# quarantine of a corrupted generation that materialize_generation() already
# performs, and it computed the revision over a differently shaped tree than the
# daemon's config sync does -- so install and sync could disagree on the
# revision for identical config.
GEN_REVISION="$(
  "$PYTHON" - "$TMP_GEN" "$MN_HOME/config.generations" "$AGENT" "$MASTER_SHA" <<'PY'
import os
import sys
import uuid
from pathlib import Path

from memory_net.shared.config.generation import materialize_generation

generation = materialize_generation(
    Path(sys.argv[1]),
    Path(sys.argv[2]),
    adapter_name=sys.argv[3],
    source_master_sha=sys.argv[4],
)
# Activate through a temporary link plus an atomic replace, so a reader never
# observes config-current missing.
current = Path(sys.argv[2]).parent / "config-current"
# A unique temporary name, removed in `finally`: a fixed one left behind by an
# interrupted run would be inherited by the next one.
tmp = current.with_name(f".config-current.{uuid.uuid4().hex}.tmp")
try:
    os.symlink(generation, tmp)
    os.replace(tmp, current)
finally:
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
print(generation.name)
PY
)"
rm -rf "$TMP_GEN"

record_stage GENERATION_READY

ADAPTER_CONFIG="$MN_HOME/config-current/adapters/$AGENT.yaml"
"$PYTHON" -m memory_net.leaf.cli hints \
  --agent-root "$AGENT_ROOT" \
  --adapter "$ADAPTER_CONFIG" \
  --state-dir "$MN_HOME/state" \
  --config-revision "$GEN_REVISION" \
  inject >/dev/null 2>&1 \
  || echo "[WARN] team hint injection did not succeed; the leaf is installed but agent-side hints are absent" >&2

record_stage HINTS_INSTALLED

# cron re-parses this string as a shell command, so every value that can
# contain a space or a metacharacter is escaped. --home and --agent-root are
# caller-supplied paths; the foreground invocation below quotes them, and the
# crontab entry has to be equally careful.
DAEMON_CMD="$(printf 'MEMORY_NET_HOME=%q %q -m memory_net.leaf.cli start --node-id %q --state-dir %q --repo %q --adapter %q --agent-root %q' \
  "$MN_HOME" "$PYTHON" "$NODE_ID" "$MN_HOME/state" "$MN_HOME/repo" "$ADAPTER_CONFIG" "$AGENT_ROOT")"
# Escape valve: the crontab write is the first of two steps that leave
# $MEMORY_NET_HOME and touch the machine itself.
if [[ "$NO_CRON" != "1" ]] && command -v crontab >/dev/null 2>&1; then
  TMP_CRON="$(mktemp)"
  crontab -l > "$TMP_CRON" 2>/dev/null || true
  if ! grep -Fq "memory-net:leaf-daemon:$NODE_ID:$MN_HOME" "$TMP_CRON"; then
    printf "%s # memory-net:leaf-daemon:%s:%s\n" "$DAEMON_CMD" "$NODE_ID" "$MN_HOME" >> "$TMP_CRON"
    crontab "$TMP_CRON"
  fi
  rm -f "$TMP_CRON"
fi

# Escape valve: the second step that leaves $MEMORY_NET_HOME. Skipping it leaves
# a fully installed but idle leaf, which is exactly what a regression wants.
if [[ "$NO_DAEMON" != "1" ]]; then
  nohup env MEMORY_NET_HOME="$MN_HOME" "$PYTHON" -m memory_net.leaf.cli start \
    --node-id "$NODE_ID" \
    --state-dir "$MN_HOME/state" \
    --repo "$MN_HOME/repo" \
    --adapter "$ADAPTER_CONFIG" \
    --agent-root "$AGENT_ROOT" \
    > "$MN_HOME/state/daemon.log" 2>&1 &
  DAEMON_PID="$!"
  echo "$DAEMON_PID" > "$MN_HOME/state/daemon.pid"
  # An install that exits non-zero must not leave a daemon behind: the operator
  # was told the install failed, and a surviving process contradicts that.
  POST_VERIFY_OK=0
  cleanup_daemon() {
    if [[ "$POST_VERIFY_OK" != "1" ]]; then
      kill "$DAEMON_PID" 2>/dev/null || true
      rm -f "$MN_HOME/state/daemon.pid"
    fi
  }
  trap cleanup_daemon EXIT

  # POST_VERIFY. Starting a process is not the same as installing a working
  # leaf: the script used to declare DAEMON_STARTED the instant nohup returned,
  # so a daemon that died on its first tick still looked like a clean install.
  # Verify two things within a bounded wait -- the daemon actually produced a
  # health snapshot, and a health file passes the strict check (which requires
  # every operational identity field, i.e. that the installed layout resolves).
  STAGE="POST_VERIFY"
  POST_VERIFY_TIMEOUT="${MEMORY_NET_INSTALL_POST_VERIFY_SECONDS:-30}"
  POST_VERIFY_HEALTH="$MN_HOME/state/post_install_health.json"
  WAITED=0
  DELAY=1
  while [[ ! -f "$MN_HOME/state/last_health.json" ]]; do
    if ! kill -0 "$DAEMON_PID" 2>/dev/null; then
      abort 6 "[ABORT] the leaf daemon exited before reporting health"
    fi
    if (( WAITED >= POST_VERIFY_TIMEOUT )); then
      abort 6 "[ABORT] the leaf daemon did not report health within ${POST_VERIFY_TIMEOUT}s"
    fi
    sleep "$DELAY"
    WAITED=$(( WAITED + DELAY ))
    if (( DELAY < 8 )); then DELAY=$(( DELAY * 2 )); fi
  done

  MEMORY_NET_HOME="$MN_HOME" "$PYTHON" -m memory_net.leaf.cli health \
    --path "$POST_VERIFY_HEALTH" \
    --node-id "$NODE_ID" >/dev/null
  MEMORY_NET_HOME="$MN_HOME" "$PYTHON" -m memory_net.leaf.cli status \
    --health "$POST_VERIFY_HEALTH" >/dev/null

  POST_VERIFY_OK=1
  record_stage DAEMON_STARTED
fi

echo "Installed memory-net leaf $NODE_ID at $MN_HOME"
