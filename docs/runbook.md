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

## Stability Check

Run `scripts/stability_smoke.py --iterations 3`; real release readiness still requires a 3-leaf 24h run.
