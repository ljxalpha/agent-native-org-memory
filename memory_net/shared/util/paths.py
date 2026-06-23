from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

MEMORIES_PREFIX = "memories"
TEAM_PREFIX = "team"
DISTILL_RUNS_PREFIX = "distill_runs"
CONFIG_PREFIX = "config"
PROMPTS_PREFIX = "prompts"

MASTER_ALLOWED_EXACT_PATHS = frozenset(
    {"bootstrap_state.json", "manifest.json", ".health.json"}
)
MASTER_ALLOWED_PREFIXES = (
    TEAM_PREFIX,
    DISTILL_RUNS_PREFIX,
    CONFIG_PREFIX,
    PROMPTS_PREFIX,
)


def expand_path(raw: str, *, agent_root: Path | None = None) -> Path:
    value = raw
    if agent_root is not None:
        value = value.replace("${AGENT_ROOT}", str(agent_root))
    value = os.path.expandvars(value)
    value = os.path.expanduser(value)
    return Path(value)


def ensure_relative_pattern(pattern: str) -> None:
    path = Path(pattern)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"glob pattern must stay relative to base_dir: {pattern}")


def safe_posix_relative_path(
    raw: str | Path,
    *,
    required_prefix: str | None = None,
) -> Path:
    value = str(raw).replace("\\", "/")
    path = PurePosixPath(value)
    parts = path.parts
    if path.is_absolute() or not parts:
        raise ValueError(f"path must be relative: {raw}")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"path must not escape its root: {raw}")
    if required_prefix is not None:
        prefix_parts = PurePosixPath(required_prefix).parts
        if parts[: len(prefix_parts)] != prefix_parts or len(parts) <= len(
            prefix_parts
        ):
            raise ValueError(f"path must be under {required_prefix}/: {raw}")
    return Path(*parts)


def ensure_within(path: Path, parent: Path) -> None:
    if not is_relative_to(path, parent):
        raise ValueError(f"path escapes parent: {path}")


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
