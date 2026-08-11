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

## Reinstalling Over a Running Leaf

The installer takes an exclusive lock on `<MEMORY_NET_HOME>/state/install.lock`,
so two installs against the same home cannot race. It does **not** coordinate
with a leaf daemon that is already running: that daemon keeps its own handle on
the repo and on `config-current` while the install replaces them.

Before reinstalling into a home that already has a daemon:

1. `memory-net-leaf stop --state-dir <MEMORY_NET_HOME>/state`, or remove the
   crontab entry and kill the pid in `state/daemon.pid`.
2. Confirm nothing remains: `pgrep -f 'memory_net.leaf.cli start'`.
3. Run the installer.

A failed install never leaves the daemon *it* started running, but it cannot
clean up one that predates it.

## What the Install Regression Does Not Cover

`tests/integration/install_leaf_local.sh` exercises the installer end to end,
but against a **local bare remote and a local-path `--package-ref`**. The
production shape — `--package-ref git+https://…@<40-char-sha>` — is deliberately
**not** in automated regression: it would make every CI run depend on network
reachability and on a published commit, and a flaky network would then look like
an installer defect.

What that leaves unproven, and how it is covered instead:

| Not exercised end to end | Covered by |
|---|---|
| pip resolving a `git+https` ref over the network | nothing automated; verify by hand before a release |
| 40-character sha enforcement for `git+*` refs | argument validation in `install_leaf.sh`, exercised by unit-level checks of the ref parser |
| rejection of floating refs (`@main`, `@master`, `@HEAD`) | same |
| rejection of credentials embedded in a URL | same |

Consequence to keep in mind: a local-path ref pins no commit, so a leaf
installed that way records `build_id_source: code_version` in
`state/build_info.json` and **cannot be listed in `approved_build_ids`**. That
is intended — see the section above.

## Enabling the Build Allowlist

`bootstrap_state.json` ships **without** an `approved_build_ids` key, and that is
deliberate. The schema's rule is "once the key exists, every installing build
must appear in it", so committing it to master fail-closes every leaf already in
the field at the moment of the push — including the ones you would need working
in order to roll back.

To turn it on:

1. Collect the build ids currently in use. Each leaf records its own in
   `<MEMORY_NET_HOME>/state/build_info.json` (`build_id` plus `build_id_source`).
2. Treat any leaf whose `build_id_source` is `code_version` as **not yet
   pinnable** — its `--package-ref` did not name a commit, so it has no build id
   to allowlist. Reinstall it from a 40-character commit ref first.
3. Add `approved_build_ids` to `bootstrap_state.json` on master containing every
   collected id **plus** the build you intend to roll out next.
4. Only then publish the new build. Installs are refused with
   `[SCHEMA_PROBE] bootstrap_state.json: ...` when the build id is not listed.
5. To disable the gate again, remove the key entirely. An empty list is not the
   same thing: it rejects every build.

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

`scripts/stability_smoke.py --iterations 3` repeats the single-pass end-to-end
regression and reports how many iterations returned zero. It is a quick "does
the flow still work" check and says nothing about any leaf's health over time.

## Three-Leaf Stability Soak

`scripts/stability_soak.py` drives `tests/integration/local_repo_three_leaves.sh`
in a loop over three independent leaves (each with its own agent root, its own
clone and its own `MEMORY_NET_HOME`) and, after every cycle, checks each leaf's
`.health.json`.

CI runs the accelerated lane on every push:

```bash
python scripts/stability_soak.py --leaves 3 --cycles 5
```

The 24h lane is **the same script, the same cycle and the same assertions** —
only the stopping condition differs:

```bash
python scripts/stability_soak.py --leaves 3 --duration-seconds 86400 \
  --workspace ~/soak-workspace --report ~/soak-report.json
```

Pass `--workspace` for any long run: it keeps the leaf repositories and the
per-cycle logs after the run instead of discarding them, which is what you need
when something fails at hour 19.

A cycle costs the same at hour 19 as at minute 1: each leaf keeps a rolling
window of three cycles' memories and each review branch is deleted once master
carries it, so neither the corpus nor the branch list grows without bound. Cycle
duration drifting upward over a long run is therefore a signal, not the setup.

### What it checks each cycle, for each leaf

1. the health file passes the strict check (`require_ops_identity=True`, the
   same seven fields as `memory-net-leaf status`);
2. `status` is `healthy`;
3. `recent_errors` is empty;
4. `last_report_at` has not moved backwards;
5. `redaction_incidents.blocked_reports` has not grown;
6. the payload changed since the previous cycle — a leaf that stopped reporting
   leaves a health file that still satisfies 1-5.

### How to read the result

- Exit code `0` and `"passed": true` in the report: every cycle checked every
  leaf and every check held.
- Any failure stops the run. The report's `failures` list names the cycle, the
  leaf `node_id` and the check. Diagnostics are deliberately field-level only:
  they never contain memory text, and `recent_errors` is reported as a count.
- `cycles_completed` lower than expected with `passed: false` means the run
  stopped early; look at the last entry of `cycles`.

### After a failure, collect

1. the report JSON (`--report`);
2. `<workspace>/logs/cycle-<n>.log` for the failing cycle — the harness output;
3. `<workspace>/leaf-work-<node_id>/.health.json` for the named leaf, and the
   same file from the cycle before it if you kept a copy;
4. `<workspace>/home-<node_id>/state/` for that leaf's install identity.

### What this lane does not prove

The three leaves are synthetic instances on one machine using the mock provider.
A green soak — accelerated or 24h — says the cycle logic and the health contract
hold. It is **not** evidence about real agents consuming memory, daemon
residency and restart, cron triggering, real embeddings or real LLM calls. A CI
run of `--cycles 5` is in particular **not** a 24h stability result.
