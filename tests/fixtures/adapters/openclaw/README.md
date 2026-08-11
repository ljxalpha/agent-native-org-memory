# OpenClaw on-disk layout fixture

Redacted snapshot of the directory layout an OpenClaw 2026.3.2 install creates,
captured read-only from a real install. Every byte of content here is neutral
sample text: no real host names, no home-directory absolute paths, no
credentials, no personal notes.

Purpose: pin the *shape* of the tree that `config/adapters/openclaw.yaml` has to
survive, so the read-path regressions in
`tests/unit/test_openclaw_adapter.py` exercise a realistic neighbourhood instead
of an invented one.

## Layout

```
<openclaw home>/
├── workspace/                 ← the agent working root (AGENT_ROOT)
│   ├── AGENTS.md              ← per-session instruction file (consumed every session)
│   ├── MEMORY.md              ← curated long-term note, agent-private
│   ├── USER.md                ← operator profile, agent-private
│   └── memory/                ← native daily notes live here...
│       └── 2026-01-01.md      ← ...as memory/YYYY-MM-DD.md, NOT under memory/shared
├── memory/
│   └── main.sqlite            ← native memory store; sibling of workspace/, i.e.
│                                one level ABOVE the managed root
├── openclaw.json              ← install config, may embed endpoints
└── _dotenv                    ← stands in for the real `.env`
```

Two things the layout is here to prove:

1. The memory-net managed root is `${AGENT_ROOT}/memory/shared`, which does not
   exist in a stock install — memory-net creates it. Everything above is
   *outside* that root and must stay uncollected.
2. The native store (`main.sqlite`) and the secret-bearing siblings
   (`_dotenv`, `credentials/`, `logs/`, `agents/`) sit at or above the parent of
   the managed root, so they are unreachable by a `${AGENT_ROOT}/memory/shared`
   relative glob even before `exclude` is considered.

## Naming deviations

* `_dotenv` — the real file is named `.env`. This repo git-ignores `.env` and
  treats a tracked file with that name as a secret-hygiene failure, so the
  fixture stores it under a neutral name. Tests that need the real name rename
  it while materializing the tree into a temp directory.
* `main.sqlite` is an intentionally **empty** placeholder. Only its name and
  position are load-bearing; no database content is reproduced.
* `credentials/`, `logs/`, `agents/` are not shipped as directories (git does
  not track empty ones); tests that need them create them on the fly.
