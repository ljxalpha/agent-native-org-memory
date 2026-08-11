"""A project-level ceiling on real model attempts (§5.5.14).

``RunBudget`` is an in-process object that ``cli.py`` rebuilds on every run, so
it cannot see a second distiller process, an agent run, or yesterday's spend. A
ceiling that resets whenever a process starts is not a ceiling on the project.

This ledger is the cross-process one: an append-only JSONL file guarded by an
``O_CREAT | O_EXCL`` lock, holding one line per *attempt* -- one request actually
put on the wire, which is what a retry loop multiplies and what the endpoint
bills. Reservations happen before the request goes out, because a record written
afterwards can report an overspend but never prevent one.

The ceiling is a constant in this file. ``AOMT_ATTEMPT_CEILING`` may only lower
it: a limit an environment variable can raise is a limit whichever process sets
that variable does not have.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from memory_net.distiller.runctx import utc_now

LOGGER = logging.getLogger(__name__)

ATTEMPT_CEILING_HARD = 50
"""The project's hard ceiling on real model API attempts. Compile-time."""

CEILING_ENV_VAR = "AOMT_ATTEMPT_CEILING"
LEDGER_ENV_VAR = "AOMT_ATTEMPT_LEDGER"
HOME_ENV_VAR = "MEMORY_NET_HOME"

KINDS = ("distill_live", "tier_b", "agent_run")
"""What an attempt was for. Free-form labels would make the sum unauditable."""

OUTCOMES = ("reserved", "ok", "failed", "refused")

_LOCK_TIMEOUT_S = 10.0
_LOCK_POLL_S = 0.01


class LedgerError(RuntimeError):
    """The ledger cannot be read, written or locked."""


class AttemptCeilingExceeded(RuntimeError):
    """The attempt was refused before any request went out."""


def attempt_ceiling(environ: Mapping[str, str] | None = None) -> int:
    """The effective ceiling: the constant, or a *lower* value from the env.

    A larger value is ignored with a warning rather than honoured. The point of
    the constant is that raising the ceiling is a code change somebody reviews.
    """
    environ = os.environ if environ is None else environ
    raw = environ.get(CEILING_ENV_VAR)
    if raw is None:
        return ATTEMPT_CEILING_HARD
    try:
        requested = int(raw)
    except ValueError:
        LOGGER.warning("ignoring non-integer %s=%r", CEILING_ENV_VAR, raw)
        return ATTEMPT_CEILING_HARD
    if requested < 0:
        LOGGER.warning("ignoring negative %s=%r", CEILING_ENV_VAR, raw)
        return ATTEMPT_CEILING_HARD
    if requested > ATTEMPT_CEILING_HARD:
        LOGGER.warning(
            "%s=%s is above the hard ceiling %s and is ignored",
            CEILING_ENV_VAR,
            requested,
            ATTEMPT_CEILING_HARD,
        )
        return ATTEMPT_CEILING_HARD
    return requested


def _clamped(ceiling: int) -> int:
    """An explicitly supplied ceiling, never above the constant."""
    if isinstance(ceiling, bool) or not isinstance(ceiling, int):
        raise LedgerError(f"attempt ceiling must be an int, got {ceiling!r}")
    if ceiling < 0:
        raise LedgerError(f"attempt ceiling cannot be negative, got {ceiling}")
    if ceiling > ATTEMPT_CEILING_HARD:
        LOGGER.warning(
            "requested attempt ceiling %s is above the hard ceiling %s and is "
            "clamped to it",
            ceiling,
            ATTEMPT_CEILING_HARD,
        )
    return min(ceiling, ATTEMPT_CEILING_HARD)


def default_ledger_path(environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    override = environ.get(LEDGER_ENV_VAR)
    if override:
        return Path(override)
    home = environ.get(HOME_ENV_VAR) or str(Path.home() / ".memory-net")
    return Path(home) / "state" / "llm_attempts.jsonl"


@dataclass(frozen=True)
class Attempt:
    """One reserved attempt, settled by the caller once the outcome is known."""

    sequence: int
    kind: str
    run_id: str


class AttemptLedger:
    """The append-only record of every attempt this project has spent."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        ceiling: int | None = None,
        now: Callable[[], str] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        environ = os.environ if environ is None else environ
        self.path = path or default_ledger_path(environ)
        # An injected ceiling is clamped by the same constant the environment
        # variable is: a construction argument that could raise it would make
        # ATTEMPT_CEILING_HARD a limit on one code path rather than on the
        # project, and the injection point exists for tests, not for headroom.
        self.ceiling = (
            attempt_ceiling(environ) if ceiling is None else _clamped(ceiling)
        )
        # The one sanctioned clock in the distiller package. The ledger's own
        # timestamps are audit metadata and never reach a product, but reading
        # the clock a second way here would be a second clock all the same.
        self._now = now or utc_now

    @property
    def lock_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".lock")

    def spent(self) -> int:
        """Attempts already recorded, recovered by replaying the file.

        Counted from the file rather than kept in memory: after a crash the
        surviving lines are the only truth about what was spent, and a counter
        that resets to zero on restart is how a ceiling gets spent twice.
        """
        return sum(1 for _ in self._entries())

    def remaining(self) -> int:
        return max(0, self.ceiling - self.spent())

    def reserve(self, *, kind: str, run_id: str, **facts: str) -> Attempt:
        """Record one attempt *before* it is made, or refuse it.

        ``facts`` may name the provider and model. Nothing about the prompt or
        the response belongs here: the ledger is a count, and a count does not
        need content to be auditable.
        """
        if kind not in KINDS:
            raise LedgerError(f"attempt kind must be one of {KINDS}, got {kind!r}")
        with self._locked():
            spent = self.spent()
            if spent >= self.ceiling:
                self._append(
                    {
                        "kind": kind,
                        "outcome": "refused",
                        "run_id": run_id,
                        "ts": self._now(),
                        **_labels(facts),
                    },
                    counted=False,
                )
                raise AttemptCeilingExceeded(
                    f"project attempt ceiling reached: {spent}/{self.ceiling}"
                )
            sequence = spent + 1
            self._append(
                {
                    "kind": kind,
                    "outcome": "reserved",
                    "run_id": run_id,
                    "sequence": sequence,
                    "ts": self._now(),
                    **_labels(facts),
                },
                counted=True,
            )
        return Attempt(sequence=sequence, kind=kind, run_id=run_id)

    def settle(self, attempt: Attempt, *, outcome: str) -> None:
        """Note how a reserved attempt ended. Never un-counts it.

        A settled failure still consumed an attempt: the request went out, and
        whether the endpoint billed it is not something this side can know.
        """
        if outcome not in OUTCOMES:
            raise LedgerError(f"outcome must be one of {OUTCOMES}, got {outcome!r}")
        with self._locked():
            self._append(
                {
                    "kind": attempt.kind,
                    "outcome": outcome,
                    "run_id": attempt.run_id,
                    "settles": attempt.sequence,
                    "ts": self._now(),
                },
                counted=False,
            )

    def entries(self) -> list[dict[str, object]]:
        return list(self._entries(counted_only=False))

    def _entries(self, *, counted_only: bool = True) -> list[dict[str, object]]:
        if not self.path.exists():
            return []
        items: list[dict[str, object]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                # A torn last line is possible after a crash. Skipping it
                # under-counts by at most one, which is the safe direction only
                # because the write that produced it never returned, so the
                # request it belonged to was never made.
                continue
            if isinstance(item, dict) and (
                not counted_only or item.get("outcome") == "reserved"
            ):
                items.append(item)
        return items

    def _append(self, record: dict[str, object], *, counted: bool) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            # Durable before the request goes out: a crash in between must lose
            # the request, never the record of it.
            os.fsync(handle.fileno())

    def _locked(self) -> _LockGuard:
        return _LockGuard(self.lock_path)


class _LockGuard:
    """An exclusive lock two *processes* respect.

    ``O_CREAT | O_EXCL`` rather than ``flock``: the file lives under
    ``MEMORY_NET_HOME``, which on some setups is a network or virtualized mount
    where advisory locks are silently no-ops.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + _LOCK_TIMEOUT_S
        while True:
            try:
                self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise LedgerError(
                        f"attempt ledger lock is held: {self.path}; remove it only "
                        "after confirming no run is in flight"
                    ) from None
                time.sleep(_LOCK_POLL_S)

    def __exit__(self, *exc_info: object) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        self.path.unlink(missing_ok=True)


def _labels(facts: Mapping[str, str]) -> dict[str, str]:
    """Short identifying labels only -- no prompt, no response, no credentials."""
    return {str(key): str(value)[:64] for key, value in facts.items()}
