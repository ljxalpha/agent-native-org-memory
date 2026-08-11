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

Run `scripts/stability_smoke.py --iterations 3`; real release readiness still requires a 3-leaf 24h run.
