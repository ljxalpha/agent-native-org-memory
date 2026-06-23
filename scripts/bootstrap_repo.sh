#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 --repo <git-url-or-path> [--source <project-root>]" >&2
}

REPO=""
SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      REPO="$2"; shift 2;;
    --source)
      SOURCE_ROOT="$2"; shift 2;;
    -h|--help)
      usage; exit 0;;
    *)
      echo "Unknown arg: $1" >&2; usage; exit 2;;
  esac
done

if [[ -z "$REPO" ]]; then
  usage; exit 2
fi

case "$REPO" in
  *oauth2:*@*|*x-access-token:*@*|*https://*:*@*|*http://*:*@*)
    echo "[ABORT] --repo must not embed credentials" >&2; exit 2;;
esac

TMPROOT="$(mktemp -d "${TMPDIR:-/tmp}/memory-net-bootstrap.XXXXXX")"
trap 'rm -rf "$TMPROOT"' EXIT

if [[ "$REPO" != *"://"* && "$REPO" != git@* && ! -e "$REPO" ]]; then
  mkdir -p "$REPO"
  git init --bare "$REPO"
fi

git clone "$REPO" "$TMPROOT/work"
cd "$TMPROOT/work"
git config user.email "memory-net@example.invalid"
git config user.name "memory-net"

if git show-ref --verify --quiet refs/remotes/origin/master; then
  git switch -C master origin/master
else
  git switch --orphan master
  git rm -rf . >/dev/null 2>&1 || true
fi

mkdir -p config prompts team distill_runs
touch team/.gitkeep distill_runs/.gitkeep
cp -R "$SOURCE_ROOT/config/." config/
cp -R "$SOURCE_ROOT/prompts/." prompts/
cp "$SOURCE_ROOT/bootstrap_state.json" bootstrap_state.json

if [[ ! -f manifest.json ]]; then
  cat > manifest.json <<'JSON'
{
  "schema_version": "memory-net/master-manifest/v1",
  "compatible_leaf_range": ">=0.1.0,<0.2.0",
  "products_active": {},
  "products_tombstones": {},
  "subject_index": {},
  "topic_migrations": {}
}
JSON
fi

NOW="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
cat > .health.json <<JSON
{
  "schema_version": "memory-net/health/v1",
  "node_id": "distiller",
  "role": "distiller",
  "status": "healthy",
  "last_heartbeat_at": "$NOW",
  "published_at": "$NOW",
  "recent_errors": [],
  "redaction_incidents": {
    "blocked_reports": 0,
    "scanner_hits": {}
  }
}
JSON

git add bootstrap_state.json config prompts manifest.json .health.json team distill_runs
if git diff --cached --quiet; then
  echo "No bootstrap changes needed"
else
  git commit -m "bootstrap memory repo"
  git push origin HEAD:master
fi
