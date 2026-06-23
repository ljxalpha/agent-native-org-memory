# Hermes Distiller Skill

Use `memory-net-distiller run --provider hermes_tool` to route distillation through a Hermes-compatible tool process.

The tool command is read from `HERMES_TOOL_COMMAND`. It receives one JSON object on stdin with `system_prompt`, `user_prompt`, `model`, `max_tokens`, and `metadata`; it must return JSON with `text`, optional `model`, and optional integer `usage` fields.

Example:

```bash
HERMES_TOOL_COMMAND="hermes-memory-distill" memory-net-distiller run --repo . --provider hermes_tool
```
