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

A cycle costs about the same at hour 19 as at minute 1: each leaf keeps a
rolling window of three cycles' memories, master keeps the two most recent
distillation run directories, and each review branch is deleted once master
carries its content. So the corpus, the run directories and the branch list stay
bounded, and cycle duration drifting upward over a long run is a signal rather
than the setup.

Two things still grow, and a long run has to budget for them:

- **`manifest.json`.** A distillation mints new product ids, so every cycle
  tombstones the previous cycle's products and the tombstone list only ever gets
  longer. Measured on this harness: roughly 3 KB of manifest per cycle, so about
  1.5 MB after a thousand cycles — and the whole file is rewritten and committed
  each time. This is the distiller's behaviour, not the harness's; the soak is
  what makes it visible at scale.
- **Git history.** Every cycle adds commits, and pruned objects stay in the
  repository until a `git gc`.

Give a 24h workspace real disk headroom (tens of gigabytes, not megabytes), put
it on a local filesystem, and watch `du -sh <workspace>` alongside the cycle
duration.

### What it checks each cycle, for each leaf

1. the health file passes the strict check (`require_ops_identity=True`, the
   same seven fields as `memory-net-leaf status`);
2. `status` is `healthy`;
3. `recent_errors` is empty;
4. `last_report_at` has not moved backwards;
5. `redaction_incidents.blocked_reports` has not grown;
6. the payload changed since the previous cycle — a leaf that stopped reporting
   leaves a health file that still satisfies 1-5.

Check 5 is the one that stays quiet on a healthy run: a report that a scanner
blocks fails the cycle before its health is ever compared, so on this lane the
counter is always zero and the comparison only earns its keep against a leaf
that goes degraded without failing. Its failure branch is covered by unit tests,
not by this lane.

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

## Running the Production Path

The production backend is DeepSeek through the OpenAI-compatible provider. It
needs two credentials, and they reach the process through `aomt-with-env`:

```sh
scripts/aomt-with-env -- memory-net-distiller run \
  --repo <checkout> \
  --config config/distill.deepseek.yaml \
  --provider deepseek
```

`config/distill.deepseek.yaml` declares `profile_class: production`, which
refuses `mock` and `hermes_tool` as effective providers. That refusal is the
point of the profile: a production run that silently fell back to a template
renderer would produce products indistinguishable from real ones at a glance.

Add `--record-dir <path>` to capture the run as a replayable bundle. The
recording directory holds **complete prompts**, so it is a private artifact:
never attach it to an issue, never upload it from CI. Only `record_metrics.json`
is safe to publish, and it is the only file the live-smoke workflow uploads.

To re-run without spending anything, replay a bundle:

```sh
memory-net-distiller run --repo <checkout> \
  --config config/distill.deepseek.yaml \
  --replay tests/fixtures/golden_bundle/v001 --replay-mode strict
```

A production profile accepts this even with no credentials present, because the
decision is made on the bundle's own per-call provenance rather than on the
configured provider name: `strict` and `prompt-only` are offline modes, and a
bundle recorded against `mock` or an unreadable bundle is still refused.

## Credential Rules

Three rules, and the tooling enforces all three rather than asking you to:

1. **Read-only.** Nothing in this repository writes to `~/.hermes/.env`, and no
   tool has a mode that edits it.
2. **Injected, never persisted.** Values travel in the `env` argument of
   `execve` and nowhere else. They are not written to a temp file, not passed on
   a command line, and not echoed. `env -i KEY=VALUE` and bwrap's `--setenv KEY
   VALUE` both put the value into argv, which any process on the machine reads
   through `/proc`; neither is used, and a test asserts neither string appears
   in the sources.
3. **Allowlisted.** The credential file is a personal store with dozens of
   unrelated keys. `aomt-with-env` reads exactly `DEEPSEEK_API_KEY` and
   `DEEPSEEK_BASE_URL` and drops the rest while parsing.

Resolution order, for both `aomt-with-env` and `agent_sandbox`:

**process environment → credential file (`~/.hermes/.env` by default) → error.**

The process environment winning is not incidental. It is the channel an operator
uses to inject a separate, rate-limited key for one run, and a shared file that
silently overrode it would send that run to the production account.

Inspecting without exposing:

```sh
scripts/aomt-with-env --check        # prints <set> or <missing>, never a value
scripts/aomt-with-env --print-keys   # prints names only
```

There is deliberately no `env` subcommand and no eval-able output mode in either
tool: both would put a credential on a terminal and into a shell history.

## AOMT_* Environment Variables

| Variable | Read by | Effect |
|---|---|---|
| `AOMT_DISTILL_PROFILE` | `memory_net/distiller/cli.py` | Overrides the run's profile class. Cannot widen a production profile into one that accepts `mock`. |
| `AOMT_OUTPUT_SCANNER_MODE` | `memory_net/distiller/output_scanner.py` | `enforce` or `observe`. `observe` records hits without failing the run; production runs use `enforce`. |
| `AOMT_ATTEMPT_CEILING` | `memory_net/distiller/llm/ledger.py` | May only **lower** the hard ceiling. A larger value is ignored, so the guard cannot be raised by an environment. |
| `AOMT_ATTEMPT_LEDGER` | `memory_net/distiller/llm/ledger.py` | Path to the attempt ledger. Point it at a scratch file when rehearsing with a fake transport, so rehearsals do not consume the real budget. |
| `AOMT_SANDBOX_ROOT` | `scripts/agent_sandbox` | Default sandbox root. Refused if it resolves inside a real agent state directory or inside this repository -- `clean` is `rm -rf` on it. |
| `AOMT_AGENT_RUNTIME_ROOT` | `scripts/agent_sandbox` | The agent's source tree, bound read-only at its own path. Required unless `--runtime-root` is given. |

The ledger is an append-only record of *attempts*, not of successes: a request
that failed still consumed rate budget, and a guard that counted only successes
would let a failing loop run forever. Read the current count before and after any
run that can reach a network.

## Verifying That an Agent Consumes the Team Hint

Writing the hint is something this project does; reading it is something the
agent does, and only the second one is worth verifying. The check runs the same
prompt twice, with the hint installed and without:

```sh
scripts/verify_adapter_consumption \
  --adapter config/adapters/hermes.yaml \
  --runtime-root <agent source tree> \
  --root <sandbox root> \
  --dry-run                       # prints the argv, runs nothing, costs nothing
```

Drop `--dry-run` to run it for real. **This spends model quota** -- two runs,
one positive and one control -- so check the ledger first and inject a separate
rate-limited key through the process environment.

The verdict is a JSON object naming what it rests on. `evidence_basis` is
`sentinel` when the agent's answer carried a token that exists only in the
injected index and the control run's answer did not, and `sentinel+trace` when a
session trace also happened to be written and agreed. A successful oneshot
usually writes no trace at all, so the `sentinel` basis is the normal passing
case and the verdict says so explicitly rather than leaving its absence to be
misread as a clean check.

Nothing about a passing check makes an adapter `stable`. That status is a claim
about a supported integration, and it needs the evidence filed alongside it.

## Agent Sandbox Mount Manifest

`scripts/agent_sandbox` runs an agent under test inside a bubblewrap namespace.
The three real agent state directories (`~/.hermes`, `~/.codex`, `~/.claude`)
are never bound: they do not exist inside the namespace at all, which is
stronger than a read-only mount, because a path that is absent cannot be read
and cannot be restored after a write.

The plan below is the contract. A test parses this block and compares it, item
for item, against what `agent_sandbox doctor` actually assembles -- so this
section cannot drift from the code without turning the suite red.

```sh
bwrap \
  --unshare-all --share-net --die-with-parent \
  --ro-bind /usr /usr \
  --ro-bind /etc /etc \
  --ro-bind ${RESOLV_TARGET} ${RESOLV_TARGET} \
  --symlink usr/bin /bin \
  --symlink usr/lib /lib \
  --symlink usr/lib64 /lib64 \
  --symlink usr/sbin /sbin \
  --ro-bind ${AGENT_RUNTIME_ROOT} ${AGENT_RUNTIME_ROOT} \
  --bind ${SANDBOX}/home /home/agent \
  --bind ${SANDBOX}/agentroot /work/agentroot \
  --tmpfs /tmp \
  --dev /dev \
  --chdir /work/agentroot \
  --
```

Notes on the entries that are easy to get wrong:

- **`/bin`, `/lib`, `/lib64`, `/sbin` are symlinks, and none is optional.**
  Without `/lib64` the dynamic loader is missing, and every command fails as
  `ENOENT` -- which reads like a wrong path rather than a missing linker.
- **`${AGENT_RUNTIME_ROOT}` is bound at its own path**, not at a new one. An
  editable install resolves imports through a finder that points at the source
  tree's absolute path, so mounting only the virtualenv yields a correct
  `sys.prefix` and no importable package.
- **`${RESOLV_TARGET}`** is present only where `/etc/resolv.conf` is a symlink
  pointing outside `/etc` (WSL2 is the common case). `--ro-bind /etc /etc` does
  not carry the target across, and without it the namespace has no DNS, so
  every model call fails as a connection error. Binding onto `/etc/resolv.conf`
  itself does not work, because `/etc` is read-only inside.
- **`/proc` is not mounted.** The agent holds the API key because it needs one,
  and with `/proc` present a read-file tool reaches `/proc/self/environ` --
  putting the key into the model's context and then into the session trace,
  which is a file we read and quote. `--allow-proc` exists, and using it
  requires a separate rate-limited key, a canary assertion that the value does
  not appear in the trace, and a scanner pass over the traces before any of
  them is filed.
- **`--bind ${SANDBOX}/agentroot`** becomes `--ro-bind` under
  `run --read-only-agentroot`. The consumption check uses that, because the
  toolset it needs includes write tools.

Credentials never appear above. They travel in the `env` argument of `execve`;
`env -i KEY=VALUE` and bwrap's `--setenv KEY VALUE` both write the value into a
command line, which any process on the machine can read through `/proc`.
`doctor` prints each credential's *source* and `<set>` or `<missing>`, and no
value. There is no `env` subcommand and no eval-able output mode.

Two environment allowlists, defined and tested separately: what the launcher
process itself may read, and the complete environment visible inside the
namespace. The second is `HOME`, `PATH`, `HERMES_HOME`, the two DeepSeek
variables, and optionally `HERMES_MAX_TOKENS`. Everything else is absent,
including the three dozen unrelated keys in the shared credential file.

Usage:

```sh
scripts/agent_sandbox --runtime-root <agent source tree> up --toolset none
scripts/agent_sandbox --runtime-root <agent source tree> run -- <command...>
scripts/agent_sandbox --runtime-root <agent source tree> doctor --since "<timestamp>"
scripts/agent_sandbox clean
```
