# hermes install snapshot (redacted)

The structure of a real hermes 0.18 home, with every byte of content replaced by
neutral sample text. Only the shape is load-bearing:

- `memories/` is the agent's **native** store, and it is *not* the managed root.
  The managed root is the memory-net convention `${AGENT_ROOT}/memory/shared`,
  and nothing under `memories/` may ever be reported.
- Each markdown file has a sibling `*.lock`. That is what the adapter's
  `**/*.lock` exclude exists for -- a lock file next to a note, not in some
  separate directory.
- `AGENTS.md` at the root is the per-session instruction file, and therefore the
  team hint target.
- `config.yaml` holds the agent's own settings and is not memory.

This file is fixture documentation and is skipped when the tree is materialized.
