"""The install's own state machine, and what it leaves behind when it fails.

``install_state.json`` used to carry exactly two values, written at two points:
``VENV_READY`` near the start and ``DAEMON_STARTED`` at the very end. Under
``set -euo pipefail`` a failure anywhere between them wrote nothing at all, so
the file left on disk described either a stale previous run or nothing --
precisely when someone needs to know how far this run got.

Two rules shape this module:

* **The failure record names a stage and an exit code, and nothing else.**
  Command output is not included. Install commands handle a remote URL, an
  absolute home path and potentially a credential fragment, and this file is
  read by support and pasted into issues. A stage name is enough to locate the
  failure; the command output is not worth the disclosure.
* **Recovery leaves evidence and never silently overwrites.** When a previous
  run left the home inconsistent, the inventory below records what was there --
  paths, sizes and content hashes, never contents -- before the install
  proceeds, so a destroyed state can still be reasoned about afterwards.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Ordered install stages. Position matters: a state naming a later stage than
#: the artifacts on disk support is what "inconsistent" means here.
INSTALL_STAGES: tuple[str, ...] = (
    "PREFLIGHT",
    "VENV_READY",
    "REPO_READY",
    "SCHEMA_PROBE",
    "BRANCH_READY",
    "GENERATION_READY",
    "HINTS_INSTALLED",
    "POST_VERIFY",
    "DAEMON_STARTED",
)
FAILED_PREFIX = "FAILED:"
#: Hashing whole generations would make recovery cost scale with the config
#: size for no benefit; the inventory only needs to identify content.
_HASH_CHUNK = 1024 * 1024


class InstallStageError(ValueError):
    """Raised when a stage name is not one of :data:`INSTALL_STAGES`."""


@dataclass(frozen=True)
class InstallState:
    """The parsed contents of ``install_state.json``."""

    status: str
    payload: dict[str, Any]

    @property
    def failed(self) -> bool:
        return self.status.startswith(FAILED_PREFIX)

    @property
    def stage(self) -> str:
        """The stage reached, whether the status is a success or a failure."""
        return self.status.removeprefix(FAILED_PREFIX)


def failed_status(stage: str) -> str:
    """Return the ``FAILED:<STAGE>`` status for ``stage``."""
    if stage not in INSTALL_STAGES:
        raise InstallStageError(f"unknown install stage: {stage}")
    return f"{FAILED_PREFIX}{stage}"


def write_state(
    state_dir: Path,
    status: str,
    *,
    exit_code: int | None = None,
    at: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Write ``install_state.json`` atomically.

    :param status: a stage name or ``FAILED:<STAGE>``.
    :param exit_code: recorded for a failure. Deliberately the *only* detail
        beyond the stage -- see the module docstring.
    :raises InstallStageError: for an unknown stage, so a typo in the install
        script cannot silently record a state nothing can interpret, or for
        extra fields on a failure -- the privacy contract above is worth
        nothing if any caller can attach command output to a failure record.
    """
    stage = status.removeprefix(FAILED_PREFIX)
    if stage not in INSTALL_STAGES:
        raise InstallStageError(f"unknown install stage: {stage}")
    if fields and status.startswith(FAILED_PREFIX):
        raise InstallStageError(
            "a failure record carries only its stage and exit code; "
            f"refusing extra fields: {sorted(fields)}"
        )
    payload: dict[str, Any] = {"status": status, **fields}
    if exit_code is not None:
        payload["exit_code"] = exit_code
    if at is not None:
        payload["at"] = at
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / "install_state.json"
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    tmp.replace(target)
    return payload


def read_state(state_dir: Path) -> InstallState | None:
    """Read ``install_state.json``, treating unreadable content as absent."""
    path = state_dir / "install_state.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("status"), str):
        return None
    return InstallState(status=payload["status"], payload=payload)


def inconsistencies(memory_net_home: Path, state: InstallState | None) -> list[str]:
    """Describe how the home disagrees with the recorded state.

    Returns reasons rather than a boolean so the evidence can say *what* was
    wrong, which is the part that is useful a week later.
    """
    reasons: list[str] = []
    if state is not None and state.failed:
        reasons.append(f"previous install failed at stage {state.stage}")
    elif state is None and _has_artifacts(memory_net_home):
        # A run that died before it could record anything, or whose state file
        # was corrupted, still leaves a venv or a repo behind. Without this the
        # most abrupt failures -- the ones most in need of evidence -- would be
        # the only ones that produce none.
        reasons.append("install artifacts exist but no readable state does")

    reached = INSTALL_STAGES.index(state.stage) if state is not None else -1
    if (
        reached >= INSTALL_STAGES.index("VENV_READY")
        and not (memory_net_home / "venv" / "bin" / "python").exists()
    ):
        reasons.append("state claims a venv that is not present")
    if (
        reached >= INSTALL_STAGES.index("REPO_READY")
        and not (memory_net_home / "repo" / ".git").exists()
    ):
        reasons.append("state claims a repo that is not present")

    current = memory_net_home / "config-current"
    if current.is_symlink() and not current.exists():
        reasons.append("config-current is a dangling symlink")
    elif reached >= INSTALL_STAGES.index("GENERATION_READY") and not current.exists():
        reasons.append("state claims a generation that is not active")
    return reasons


def _has_artifacts(memory_net_home: Path) -> bool:
    """True when a *previous* run got past the point recovery is checked at.

    The venv is deliberately not in this list. It is built before the recovery
    check runs, so counting it would make every clean first install look like a
    dirty one -- and a recovery notice that fires on every install teaches
    operators to ignore recovery notices.
    """
    return any(
        path.exists() or path.is_symlink()
        for path in (
            memory_net_home / "repo",
            memory_net_home / "local.yaml",
            memory_net_home / "config-current",
        )
    )


def record_dirty_recovery(
    memory_net_home: Path,
    *,
    reasons: list[str],
    timestamp: str,
) -> Path:
    """Record an inventory of what a previous run left behind.

    Only paths, sizes and content hashes are recorded -- never file contents.
    An inventory is small enough to keep forever, whereas copying state would
    duplicate whatever secrets and machine-specific paths live in it.

    :returns: the directory holding ``inventory.json``.
    """
    # Unique per call: the timestamp has second precision, and evidence that
    # silently overwrites earlier evidence is worse than none, because it looks
    # complete.
    root = memory_net_home / "state" / "dirty_recovery"
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"{timestamp}-{uuid.uuid4().hex[:8]}"
    destination.mkdir()
    inventory = {
        "at": timestamp,
        "reasons": reasons,
        "entries": [
            _entry(memory_net_home, path)
            for path in _inventory_targets(memory_net_home)
        ],
    }
    (destination / "inventory.json").write_text(
        json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return destination


def _inventory_targets(memory_net_home: Path) -> list[Path]:
    targets = [
        memory_net_home / "local.yaml",
        memory_net_home / "config-current",
        memory_net_home / "state" / "install_state.json",
        memory_net_home / "state" / "install_id",
        memory_net_home / "state" / "build_info.json",
    ]
    return [path for path in targets if path.exists() or path.is_symlink()]


def _entry(memory_net_home: Path, path: Path) -> dict[str, Any]:
    entry: dict[str, Any] = {"path": str(path.relative_to(memory_net_home))}
    if path.is_symlink():
        entry["kind"] = "symlink"
        entry["target_exists"] = path.exists()
        return entry
    if path.is_dir():
        entry["kind"] = "directory"
        return entry
    entry["kind"] = "file"
    entry["size"] = path.stat().st_size
    entry["sha256"] = _sha256(path)
    return entry


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    """CLI shim the install script calls at each stage boundary."""
    parser = argparse.ArgumentParser(prog="memory-net-install-state")
    sub = parser.add_subparsers(dest="action", required=True)

    record = sub.add_parser("record")
    record.add_argument("--state-dir", required=True, type=Path)
    record.add_argument("--status", required=True)
    record.add_argument("--exit-code", type=int)
    record.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help=(
            "extra context for a *success* record, e.g. hints-injected=false. "
            "Refused on a failure record, whose shape is fixed"
        ),
    )

    recover = sub.add_parser("recover")
    recover.add_argument("--home", required=True, type=Path)

    args = parser.parse_args(argv)
    if args.action == "record":
        write_state(
            args.state_dir,
            args.status,
            exit_code=args.exit_code,
            at=_utc_now(),
            **_parse_fields(args.field),
        )
        return 0

    reasons = inconsistencies(args.home, read_state(args.home / "state"))
    if not reasons:
        return 0
    destination = record_dirty_recovery(
        args.home,
        reasons=reasons,
        timestamp=_utc_now().replace(":", "-"),
    )
    # Named on stderr so the operator knows evidence was kept; the install then
    # continues, because refusing here would leave no way to repair the home.
    print(
        f"[RECOVERY] previous install left this home inconsistent "
        f"({'; '.join(reasons)}); inventory recorded at "
        f"{destination.relative_to(args.home)}",
        file=sys.stderr,
    )
    return 0


def _parse_fields(raw_fields: list[str]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for raw in raw_fields:
        key, separator, value = raw.partition("=")
        if not separator or not key:
            raise InstallStageError(f"field must be KEY=VALUE: {raw}")
        fields[key.replace("-", "_")] = _coerce(value)
    return fields


def _coerce(value: str) -> Any:
    if value in {"true", "false"}:
        return value == "true"
    return value


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
