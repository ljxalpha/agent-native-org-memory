# Tuning Loop

1. Record a baseline distill run with `--record`.
2. Adjust prompts in `prompts/`.
3. Replay with `--replay-mode prompt-only` or `allow-live-on-miss`.
4. Compare product text with `scripts/prompt_ab.py` and render with `scripts/prompt_ab_view.py`.
5. Check golden eval and run metrics separately before promotion.

The current MVP tools provide readable text diffs only. Structured frontmatter,
citation, quality, and cost comparisons are a planned follow-up extension.

## Refreshing the Golden Bundle

`tests/fixtures/golden_bundle/` holds one recorded production run. Every test
that exercises the production path without spending anything replays it, so the
bundle is not a cache: it is a published test asset, and it ships to anyone who
clones the repository.

Bundles are versioned directories (`v001`, `v002`, ...) with a `CURRENT` file
naming the active one. A refresh **adds** a version and repoints `CURRENT`; it
never edits a published one. The old directory stays until a separate decision
removes it, so a regression can be bisected across the change.

Refresh only when there is a reason -- a changed prompt, a changed schema, a
provider that returns a different shape. Re-recording to "get fresh output"
spends live quota to reduce reproducibility.

### The procedure

1. Build the golden input repository, which is deterministic and synthetic:
   `python tests/fixtures/golden_repo/build_repo.py --out <path>`.
2. Read the attempt ledger and note the count. Recording spends live quota.
3. Record into a **staging directory outside the repository**, so a failed or
   half-reviewed recording can never be committed by accident:
   `scripts/aomt-with-env -- memory-net-distiller run --repo <path> --config
   config/distill.deepseek.yaml --provider deepseek --record-dir <staging>`.
4. Read the ledger again and record the actual number of attempts consumed.
5. Run both gates below.
6. Copy the reviewed bundle to `tests/fixtures/golden_bundle/vNNN/`, update
   `CURRENT`, and run the replay regression. It must be green with
   `replay_live_calls == 0`.

### Two gates, and both are required

**Gate one, the scanner.** Every file in the bundle passes the structured output
scanner in `enforce` mode. This is mechanical and it runs in the suite.

**Gate two, human review, file by file.** The scanner finds the shapes it has
patterns for. A bundle contains complete prompts and complete model responses,
which is the highest-density leak surface in the repository, and the things that
matter most there have no shape: a real project name, an internal service, an
operator's phrasing of a real incident. Someone reads every file and records the
conclusion.

The gates are not redundant, and passing the first is not grounds for skipping
the second. Fixture inputs must be synthetic neutral content throughout -- never
real memory records, even redacted ones, because redaction is a judgement about
what is identifying and a fixture is a place where that judgement is not needed.

### What is in a bundle

`calls.jsonl` (one line per call, with per-call provenance), `requests/` (the
full prompt of each call, hashed into the filename), `cluster_assignment.json`,
`snapshot.json`, and `record_metrics.json`. Only the last of these is safe to
publish outside the repository -- it carries counts and provenance, no text --
and it is the only file CI uploads.

`record_metrics.json` is also where a refresh is checked at a glance:
`provider_backends` must name the real backend, `usage_source` must be
`reported` rather than `estimated`, and the call count must match `calls.jsonl`.
A bundle whose backend reads `mock` or `unknown` is refused by the production
profile at replay time.
