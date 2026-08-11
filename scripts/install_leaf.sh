#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage: install_leaf.sh --repo <memory-repo> --agent <agent> --node-id <id> --agent-root <path> --package-ref <ref>
USAGE
}

MEMORY_REPO=""
AGENT="claude-code"
NODE_ID=""
AGENT_ROOT=""
PACKAGE_REF=""
MN_HOME="${MEMORY_NET_HOME:-$HOME/.memory-net}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) MEMORY_REPO="$2"; shift 2;;
    --agent) AGENT="$2"; shift 2;;
    --node-id) NODE_ID="$2"; shift 2;;
    --agent-root) AGENT_ROOT="$2"; shift 2;;
    --package-ref) PACKAGE_REF="$2"; shift 2;;
    --home) MN_HOME="$2"; shift 2;;
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
  [[ "$EXISTING" == "$MEMORY_REPO" ]] || {
    echo "[ABORT] existing repo origin differs: $EXISTING" >&2; exit 4;
  }
fi

cd "$MN_HOME/repo"
git fetch origin
git show origin/master:bootstrap_state.json >/dev/null
git show origin/master:manifest.json >/dev/null
ADAPTER_YAML="$(git show "origin/master:config/adapters/$AGENT.yaml")"
if grep -Eq '^support_status:[[:space:]]*experimental[[:space:]]*$' <<<"$ADAPTER_YAML"; then
  if [[ "${MEMORY_NET_ALLOW_EXPERIMENTAL_ADAPTER:-}" != "1" ]]; then
    echo "[ABORT] adapter '$AGENT' is experimental; set MEMORY_NET_ALLOW_EXPERIMENTAL_ADAPTER=1 to opt in" >&2
    exit 4
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
    echo "[ABORT] leaf/$NODE_ID is owned by another install" >&2
    exit 5
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

cat > "$MN_HOME/state/install_state.json" <<JSON
{"status":"VENV_READY","node_id":"$NODE_ID","agent":"$AGENT"}
JSON

TMP_GEN="$(mktemp -d "$MN_HOME/config.generations/.gen.XXXXXX")"
mkdir -p "$TMP_GEN"
# Snapshot provenance: which master commit this generation was exported from.
# It is recorded as a marker inside the generation, never as hashed content.
MASTER_SHA="$(git rev-parse origin/master)"
git --work-tree="$TMP_GEN" checkout origin/master -- bootstrap_state.json config prompts >/dev/null
cp "$TMP_GEN/bootstrap_state.json" "$TMP_GEN/config/bootstrap_state.json"
cp -R "$TMP_GEN/prompts" "$TMP_GEN/config/prompts"
GEN_REVISION="$(
  "$PYTHON" - <<'PY' "$TMP_GEN" "$TMP_GEN/config" "$AGENT" "$MASTER_SHA"
import sys
from pathlib import Path

from memory_net.shared.config.generation import (
    canonical_generation_hash,
    subscribe_contract_revision,
)

source_root = Path(sys.argv[1])
root = Path(sys.argv[2])
agent = sys.argv[3]
source_master_sha = sys.argv[4]
(root / "config_revision").write_text(
    canonical_generation_hash(root),
    encoding="utf-8",
)
(root / "subscribe_contract_revision").write_text(
    subscribe_contract_revision(source_root, adapter_name=agent),
    encoding="utf-8",
)
(root / "config_source_master_sha").write_text(
    source_master_sha,
    encoding="utf-8",
)
print(canonical_generation_hash(root))
PY

)"
GEN_DIR="$MN_HOME/config.generations/$GEN_REVISION"
if [[ ! -d "$GEN_DIR" ]]; then
  mv "$TMP_GEN/config" "$GEN_DIR"
fi
rm -rf "$TMP_GEN"
ln -sfn "$GEN_DIR" "$MN_HOME/config-current"

ADAPTER_CONFIG="$MN_HOME/config-current/adapters/$AGENT.yaml"
"$PYTHON" -m memory_net.leaf.cli hints \
  --agent-root "$AGENT_ROOT" \
  --adapter "$ADAPTER_CONFIG" \
  --state-dir "$MN_HOME/state" \
  --config-revision "$GEN_REVISION" \
  inject >/dev/null 2>&1 || true

DAEMON_CMD="MEMORY_NET_HOME=$MN_HOME $PYTHON -m memory_net.leaf.cli start --node-id $NODE_ID --state-dir $MN_HOME/state --repo $MN_HOME/repo --adapter $ADAPTER_CONFIG --agent-root $AGENT_ROOT"
if command -v crontab >/dev/null 2>&1; then
  TMP_CRON="$(mktemp)"
  crontab -l > "$TMP_CRON" 2>/dev/null || true
  if ! grep -Fq "memory-net:leaf-daemon:$NODE_ID:$MN_HOME" "$TMP_CRON"; then
    printf "%s # memory-net:leaf-daemon:%s:%s\n" "$DAEMON_CMD" "$NODE_ID" "$MN_HOME" >> "$TMP_CRON"
    crontab "$TMP_CRON"
  fi
  rm -f "$TMP_CRON"
fi

nohup env MEMORY_NET_HOME="$MN_HOME" "$PYTHON" -m memory_net.leaf.cli start \
  --node-id "$NODE_ID" \
  --state-dir "$MN_HOME/state" \
  --repo "$MN_HOME/repo" \
  --adapter "$ADAPTER_CONFIG" \
  --agent-root "$AGENT_ROOT" \
  > "$MN_HOME/state/daemon.log" 2>&1 &
echo "$!" > "$MN_HOME/state/daemon.pid"

cat > "$MN_HOME/state/install_state.json" <<JSON
{"status":"DAEMON_STARTED","node_id":"$NODE_ID","agent":"$AGENT","generation":"$GEN_REVISION"}
JSON

echo "Installed memory-net leaf $NODE_ID at $MN_HOME"
