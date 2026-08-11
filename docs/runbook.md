# Agent-Native Organizational Memory Architecture (aomt) Runbook

## Distiller Failure

1. Check `.health.json` for `status=degraded` and `recent_errors`.
2. Read `distill_runs/latest/metrics.json` for failed run context.
3. If push failed, inspect `distill_runs/<run_id>/push_failed/`.
4. Re-run with `--no-push` before attempting another publish.

## Redaction Incident

1. Stop leaf daemon or run with `--no-push`.
2. Review `state/scanner_audit/*.jsonl`; entries never include raw secret values.
3. Add or tighten rules in `config/redaction_patterns.yaml` and run `scripts/redaction_benchmark.py`.
4. Re-report after rules pass benchmark.

## Leaf Health Check

`memory-net-leaf status --health <path>` reads, validates and prints a health
file. It exits `1` (and names the offending fields on stderr) instead of
printing an unusable payload.

1. Point it at the leaf repo's `.health.json`, or at
   `<MEMORY_NET_HOME>/state/last_health.json` for the daemon's own snapshot.
2. It is **strict by default**: the seven operational identity fields
   (`adapter`, `install_id`, `code_version`, `host`, `build_id`,
   `config_source_master_sha`, `started_at`) must all be present, because a
   health file that cannot be traced to a machine and an install is not
   actionable during an incident.
3. `--lenient` accepts a file written before those fields existed. Use it for an
   installation that has not been reinstalled yet; the durable fix is to let the
   leaf write a fresh health file.
4. Missing identity on a *new* install means the layout is incomplete —
   check `<MEMORY_NET_HOME>/state/install_id`, `local.yaml` and the
   `config-current` symlink before looking anywhere else.

## Stability Check

Run `scripts/stability_smoke.py --iterations 3`; real release readiness still requires a 3-leaf 24h run.
