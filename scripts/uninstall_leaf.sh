#!/usr/bin/env bash
set -euo pipefail

MN_HOME="${MEMORY_NET_HOME:-$HOME/.memory-net}"
PURGE_REPO=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --home) MN_HOME="$2"; shift 2;;
    --keep-repo) PURGE_REPO=false; shift;;
    --purge-repo) PURGE_REPO=true; shift;;
    -h|--help)
      echo "Usage: $0 [--home <path>] [--keep-repo] [--purge-repo]" >&2; exit 0;;
    *) echo "Unknown arg: $1" >&2; exit 2;;
  esac
done

if [[ ! -d "$MN_HOME" ]]; then
  echo "Nothing to uninstall at $MN_HOME"
  exit 0
fi

STATE_DIR="$MN_HOME/state"
mkdir -p "$STATE_DIR"
python3 -m memory_net.leaf.cli stop --state-dir "$STATE_DIR" >/dev/null 2>&1 || {
  printf "stop requested\n" > "$STATE_DIR/stop-requested"
}

LOCAL_YAML="$MN_HOME/local.yaml"
AGENT_ROOT=""
if [[ -f "$LOCAL_YAML" ]]; then
  AGENT_ROOT="$(python3 - <<'PY' "$LOCAL_YAML"
import sys
from pathlib import Path

path = Path(sys.argv[1])
for line in path.read_text(encoding="utf-8").splitlines():
    if not line.startswith("agent_root:"):
        continue
    value = line.split(":", 1)[1].strip()
    print(value.strip('"').strip("'"))
    break
PY
)"
fi

ADAPTER_CONFIG=""
if [[ -d "$MN_HOME/config-current/adapters" ]]; then
  ADAPTER_CONFIG="$(find "$MN_HOME/config-current/adapters" -maxdepth 1 -type f -name '*.yaml' -print -quit)"
fi
if [[ -n "$AGENT_ROOT" && -n "$ADAPTER_CONFIG" ]]; then
  python3 -m memory_net.leaf.cli hints \
    --agent-root "$AGENT_ROOT" \
    --adapter "$ADAPTER_CONFIG" \
    --state-dir "$MN_HOME/state" \
    remove >/dev/null 2>&1 || true
fi

if command -v crontab >/dev/null 2>&1; then
  TMP_CRON="$(mktemp)"
  if crontab -l > "$TMP_CRON" 2>/dev/null; then
    python3 - <<'PY' "$TMP_CRON" "$MN_HOME" | crontab -
import sys
from pathlib import Path

path = Path(sys.argv[1])
home = sys.argv[2]
for line in path.read_text(encoding="utf-8").splitlines():
    tagged = "memory-net:leaf-daemon" in line
    anchored = home in line or f"MEMORY_NET_HOME={home}" in line
    command = "memory-net-leaf start" in line or "memory_net.leaf.cli start" in line
    if command and (tagged or anchored):
        continue
    print(line)
PY
  fi
  rm -f "$TMP_CRON"
fi

rm -rf "$MN_HOME/config.generations" "$MN_HOME/config-current" "$MN_HOME/local.yaml"
find "$STATE_DIR" -mindepth 1 \
  ! -name install_id \
  ! -name stop-requested \
  -exec rm -rf {} +

if [[ "$PURGE_REPO" == true ]]; then
  rm -rf "$MN_HOME/repo"
fi

echo "Uninstalled memory-net leaf files from $MN_HOME"
