# codex on-disk layout fixture

Redacted snapshot of the directory shape a codex install creates, captured
read-only from a real install. Every byte of content here is neutral sample
text: no real host names, no home-directory absolute paths, no credentials, no
personal notes.

Purpose: pin the *shape* that `config/adapters/codex.yaml` has to survive, so
the read-path regressions in `tests/unit/test_adapter_calibration.py` exercise a
realistic neighbourhood instead of an invented one.

## Layout

```
<codex home>/                  ← AGENT_ROOT
├── memories/                  ← the agent's NATIVE store, not the managed root
│   ├── project-notes.md       ← a native note: markdown, inside AGENT_ROOT,
│   │                            and still not collectable
│   ├── .git/                  ← the store is a git checkout
│   ├── .agents/               ← empty in a stock install
│   └── .codex/                ← empty in a stock install
├── memories_1.sqlite          ← native index; a SIBLING of memories/, one level
├── memories_1.sqlite-shm        above the store, alongside its WAL pair
└── memories_1.sqlite-wal
```

The managed root is the memory-net convention `${AGENT_ROOT}/memory/shared`,
which a stock install does not have — memory-net creates it. Everything above is
outside that root.

Three things the layout is here to prove:

1. `memories/` is *not* the managed root. It is markdown, it is inside
   AGENT_ROOT, and nothing in it may ever be reported. Position decides this,
   before any `exclude` is consulted.
2. The `.git/` exclude is not decorative. The native store is a real checkout,
   and the same thing happens inside the managed root the moment an operator
   versions it — which is why the pattern is both `.git/**` and `**/.git/**`.
3. There is deliberately **no** `**/*.sqlite*` exclude. The sqlite trio sits
   beside `memories/`, one level above the managed root, so a relative glob
   rooted at `memory/shared` cannot reach it. An exclude would be a second
   mechanism agreeing with the first, and it would suggest that a `.sqlite`
   *inside* the managed root is expected — it is not.

## Naming deviations

* `_dotgit/`, `_dotagents/`, `_dotcodex/` — the real names are `.git`,
  `.agents`, `.codex`. A tracked directory named `.git` inside this repository
  would be read as a nested checkout and its contents could not be committed at
  all, so the fixture stores neutral names. Tests rename them while
  materializing the tree into a temp directory.
* `.keep` files stand in for directories git cannot track empty. They are
  dropped during materialization, so what the tests see is the empty directory a
  stock install actually has.
* The `.sqlite` trio are intentionally **empty** placeholders. Only their names
  and position are load-bearing; no database content is reproduced. The `-shm`
  and `-wal` pair exists only while a connection is open — measured on a settled
  install only `memories_1.sqlite` was present — but a snapshot taken mid-run
  has all three, and that is the state the read path has to survive.

This file is fixture documentation and is skipped when the tree is materialized.
