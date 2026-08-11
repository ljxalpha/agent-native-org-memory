# Hermes Distiller Skill

`hermes_tool` is an **experimental integration entry point**, not a production
backend. It exists so that a Hermes-compatible tool process can be wired into
the distiller and exercised end to end. Its output quality is whatever the tool
behind it produces, and in the shipped configuration that tool is a stub.

**The production backend is `--provider deepseek`.** See
[the runbook](../runbook.md#running-the-production-path) for how to run it.
`config/distill.deepseek.yaml` declares `profile_class: production`, and a
production profile **refuses** both `mock` and `hermes_tool` as effective
providers. That refusal is deliberate: a run that silently fell back to a
template renderer would produce products that look real, and the distinction
between "the model wrote this" and "a format string wrote this" is the whole
value of the output.

For the same reason, `hermes_tool` is not a baseline for cross-backend
comparison. Whatever sits behind it is not the model, so comparing it against
the mock lane compares the mock lane with itself. The equivalence contract uses
the mock lane against a strict replay of a real recorded run.

## Using it

The tool command is read from `HERMES_TOOL_COMMAND`. It receives one JSON object
on stdin with `system_prompt`, `user_prompt`, `model`, `max_tokens`, and
`metadata`. It must return a JSON object carrying `text`; `model`, integer
`usage`, `usage_source`, `provider_backend` and a string-valued `raw_meta` are
all optional.

```bash
HERMES_TOOL_COMMAND="hermes-memory-distill" \
  memory-net-distiller run --repo . --provider hermes_tool
```

The last three of those keys are the provenance the recording will carry, and
they are **declared by the tool**, not observed by this side of the bridge:

- `usage_source` is accepted only if it is one of `reported` / `estimated` /
  `unknown`. A tool that declares nothing, or declares something else, records
  `unknown` — never an optimistic fallback to `reported`.
- `provider_backend` defaults to `hermes_tool` when the tool says nothing, but a
  tool that names something else is taken at its word. The bridge cannot verify
  the claim; what it can do is refuse to invent one.

A production profile refuses to replay a bundle whose calls name `hermes_tool`,
`mock`, or `unknown` — a bundle is trusted on its own per-call provenance, not
on the name of the config that replays it.
