#!/usr/bin/env python3
"""Three-leaf stability soak driver.

``tests/integration/local_repo.sh`` proves the end-to-end flow works *once*.
This driver repeats the three-leaf harness
(``tests/integration/local_repo_three_leaves.sh``) against one accumulating
workspace and, after every cycle, asserts the health contract of **each** leaf:

1. ``validate_health(..., require_ops_identity=True)`` passes;
2. ``status == "healthy"``;
3. ``recent_errors`` is empty;
4. ``last_report_at`` does not go backwards;
5. ``redaction_incidents.blocked_reports`` does not grow;
6. the payload actually changed since the previous cycle.

Check 6 is what keeps the other five from passing vacuously: a health file that
was never rewritten satisfies 1-5 forever, so without it "the leaf is alive"
would be indistinguishable from "the file is still on disk".

The accelerated lane (``--cycles 5``, run in CI) and the operator's 24h lane
(``--duration-seconds 86400``) are the same code path with different stopping
conditions; nothing about a cycle changes between them.

Diagnostics deliberately carry no memory text: failures record the cycle, the
node id, a check name and a short contract-level detail. Harness output goes to
a log file inside the workspace and never into the report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from memory_net.shared.schema import SchemaError, validate_health
from memory_net.shared.schema.validation import HEALTH_OPS_IDENTITY_FIELDS

SCHEMA_VERSION = "memory-net/stability-soak/v1"
DEFAULT_SCRIPT = Path("tests/integration/local_repo_three_leaves.sh")
DEFAULT_LEAVES = 3
DEFAULT_CYCLES = 3

#: Details the driver builds itself (counters, closed-domain values) are safe by
#: construction. Details that quote an upstream message are not: a validator
#: renders the offending *value* into its message, and a corrupted health file
#: can hold anything, including memory text. Such details go through
#: :func:`sanitize_detail`.
_DETAIL_ALLOWED = re.compile(r"[^A-Za-z0-9_,.()/ -]")
_DETAIL_MAX_LENGTH = 160
#: Every character a validator uses to introduce an interpolated value. The
#: message is cut at the first one: `status=<value>`, `scanner_hits[<key>]`,
#: `fields: <list>`. Cutting at `=` alone would let a bracketed key through.
_VALUE_INTRODUCERS = "=[:"
#: The closed value domain of ``status``; anything else is named, never echoed.
_KNOWN_STATUSES = frozenset({"healthy", "degraded"})


def sanitize_detail(text: str) -> str:
    """Reduce an upstream diagnostic string to a bounded, value-free fragment.

    The message is truncated at the first character a validator uses to
    introduce a value, so nothing that came from the health file survives. The
    one exception is deliberately built from this repository's own constants:
    when the message names operational identity fields, the *intersection* with
    :data:`HEALTH_OPS_IDENTITY_FIELDS` is appended, so the operator still learns
    which field is missing while the emitted text can only ever be a literal
    from our own code.
    """
    collapsed = " ".join(text.split())
    cuts = [index for index in map(collapsed.find, _VALUE_INTRODUCERS) if index != -1]
    head = collapsed[: min(cuts)] if cuts else collapsed
    named = [field for field in HEALTH_OPS_IDENTITY_FIELDS if field in collapsed]
    if named:
        head = f"{head} ({', '.join(named)})"
    filtered = _DETAIL_ALLOWED.sub("?", head).strip()
    if len(filtered) <= _DETAIL_MAX_LENGTH:
        return filtered
    return filtered[:_DETAIL_MAX_LENGTH] + "..."


@dataclass(frozen=True)
class LeafRef:
    """One leaf the harness drives, and where it writes its health file."""

    node_id: str
    health_path: Path


@dataclass(frozen=True)
class HealthRead:
    """The result of reading one leaf's ``.health.json`` for one cycle."""

    payload: dict[str, Any] | None
    digest: str
    error: str | None = None


@dataclass(frozen=True)
class LeafSnapshot:
    """The cross-cycle state one leaf carries into the next comparison."""

    last_report_at: str
    blocked_reports: int
    digest: str


@dataclass(frozen=True)
class Failure:
    cycle: int
    node_id: str
    check: str
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "cycle": self.cycle,
            "node_id": self.node_id,
            "check": self.check,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CycleExecution:
    """What one harness invocation produced, before any assertion is applied."""

    returncode: int
    duration_seconds: float
    log_relative_path: str
    healths: dict[str, HealthRead] = field(default_factory=dict)


#: A cycle executor maps a cycle number to its execution result. Injecting it is
#: what lets the assertions above be tested without running the harness.
CycleExecutor = Callable[[int], CycleExecution]


def read_health(path: Path) -> HealthRead:
    """Read one health file, keeping both the payload and its exact bytes.

    The digest is taken over the raw bytes rather than the parsed payload so
    that "was this file rewritten?" is answered by the file itself.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return HealthRead(
            payload=None, digest="", error=f"unreadable: {type(exc).__name__}"
        )
    digest = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return HealthRead(
            payload=None, digest=digest, error=f"invalid JSON: {type(exc).__name__}"
        )
    if not isinstance(payload, dict):
        return HealthRead(payload=None, digest=digest, error="payload is not an object")
    return HealthRead(payload=payload, digest=digest)


def _parse_timestamp(value: str) -> datetime | None:
    try:
        # Python 3.11+ parses the trailing ``Z`` that health timestamps carry.
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def evaluate_leaf(
    *,
    cycle: int,
    node_id: str,
    read: HealthRead,
    previous: LeafSnapshot | None,
) -> tuple[list[Failure], LeafSnapshot | None]:
    """Apply the six health checks to one leaf for one cycle.

    :returns: ``(failures, snapshot)``. The snapshot is what the next cycle
        compares against; it is ``None`` when this cycle produced nothing
        comparable, so a broken cycle cannot silently reset the baseline.
    """
    failures: list[Failure] = []

    def fail(check: str, detail: str = "") -> None:
        failures.append(
            Failure(cycle=cycle, node_id=node_id, check=check, detail=detail)
        )

    payload = read.payload
    if payload is None:
        fail("health_unreadable", sanitize_detail(read.error or ""))
        return failures, None

    try:
        validate_health(payload, require_ops_identity=True)
    except SchemaError as exc:
        fail("health_schema", sanitize_detail(str(exc)))

    # The leaf the harness said this file belongs to must be the leaf that wrote
    # it. Without this, a harness that pointed three node ids at one healthy file
    # would report three green leaves.
    if payload.get("node_id") != node_id:
        fail("node_id_mismatch", "health file belongs to another leaf")

    status = payload.get("status")
    if status != "healthy":
        known = isinstance(status, str) and status in _KNOWN_STATUSES
        fail("status", f"status={status}" if known else "status=<unexpected>")

    recent_errors = payload.get("recent_errors") or []
    if recent_errors:
        # The count, never the strings: they are assembled upstream and cannot
        # be assumed free of memory fragments.
        fail("recent_errors", f"count={len(recent_errors)}")

    incidents = payload.get("redaction_incidents")
    blocked_reports = 0
    if isinstance(incidents, dict) and isinstance(
        incidents.get("blocked_reports"), int
    ):
        blocked_reports = int(incidents["blocked_reports"])

    raw_last_report_at = payload.get("last_report_at")
    last_report_at = raw_last_report_at if isinstance(raw_last_report_at, str) else ""
    parsed = _parse_timestamp(last_report_at) if last_report_at else None
    if not last_report_at:
        fail("last_report_at_missing")
    elif parsed is None:
        fail("last_report_at_unparsable")

    if previous is not None:
        previous_parsed = _parse_timestamp(previous.last_report_at)
        if (
            parsed is not None
            and previous_parsed is not None
            and parsed < previous_parsed
        ):
            fail("last_report_at_regressed", "current < previous")
        if blocked_reports > previous.blocked_reports:
            fail(
                "blocked_reports_increased",
                f"{previous.blocked_reports}->{blocked_reports}",
            )
        if read.digest == previous.digest:
            fail("health_not_regenerated", "payload identical to previous cycle")

    snapshot = LeafSnapshot(
        last_report_at=last_report_at,
        blocked_reports=blocked_reports,
        digest=read.digest,
    )
    return failures, snapshot


def evaluate_cycle(
    *,
    cycle: int,
    leaves: Sequence[LeafRef],
    execution: CycleExecution,
    snapshots: dict[str, LeafSnapshot],
) -> list[Failure]:
    """Check one cycle's harness result and every leaf's health file."""
    failures: list[Failure] = []
    if execution.returncode != 0:
        failures.append(
            Failure(
                cycle=cycle,
                node_id="-",
                check="harness_failed",
                detail=f"returncode={execution.returncode}",
            )
        )
        return failures
    if not leaves:
        # A cycle that checked nothing must not read as a green cycle.
        failures.append(Failure(cycle=cycle, node_id="-", check="no_leaves", detail=""))
        return failures
    for leaf in leaves:
        read = execution.healths.get(
            leaf.node_id,
            HealthRead(payload=None, digest="", error="no health file was collected"),
        )
        leaf_failures, snapshot = evaluate_leaf(
            cycle=cycle,
            node_id=leaf.node_id,
            read=read,
            previous=snapshots.get(leaf.node_id),
        )
        failures.extend(leaf_failures)
        if snapshot is not None:
            snapshots[leaf.node_id] = snapshot
    return failures


class HarnessCycleExecutor:
    """Run one harness cycle and collect every leaf's health file.

    Harness stdout/stderr is written to ``<workspace>/logs/cycle-<n>.log``. It
    is never returned: the harness prints repository paths and distiller output,
    and the report is an artifact that gets uploaded.
    """

    def __init__(
        self,
        *,
        script: Path,
        workspace: Path,
        expected_leaves: int,
        runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._script = script
        self._workspace = workspace
        self._expected_leaves = expected_leaves
        self._runner = runner
        self._clock = clock
        self.leaves: list[LeafRef] = []

    def __call__(self, cycle: int) -> CycleExecution:
        logs_dir = self._workspace / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = logs_dir / f"cycle-{cycle}.log"
        started = self._clock()
        with log_path.open("wb") as log_file:
            completed = self._runner(
                [
                    "bash",
                    str(self._script),
                    "--workspace",
                    str(self._workspace),
                    "--cycle",
                    str(cycle),
                ],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                check=False,
            )
        duration = round(self._clock() - started, 3)
        log_relative_path = str(log_path.relative_to(self._workspace))
        if completed.returncode != 0:
            return CycleExecution(
                returncode=completed.returncode,
                duration_seconds=duration,
                log_relative_path=log_relative_path,
            )
        self.leaves = self._read_leaves()
        return CycleExecution(
            returncode=0,
            duration_seconds=duration,
            log_relative_path=log_relative_path,
            healths={
                leaf.node_id: read_health(leaf.health_path) for leaf in self.leaves
            },
        )

    def _read_leaves(self) -> list[LeafRef]:
        manifest_path = self._workspace / "leaves.json"
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        leaves = [
            LeafRef(
                node_id=str(item["node_id"]), health_path=Path(str(item["health_path"]))
            )
            for item in document["leaves"]
        ]
        # Distinct leaves must be distinct in both directions: a repeated node id
        # or a shared health file would make one observation look like several.
        if len({leaf.node_id for leaf in leaves}) != len(leaves):
            raise SystemExit("the harness declared the same node id twice")
        if len({leaf.health_path.resolve() for leaf in leaves}) != len(leaves):
            raise SystemExit("the harness pointed two leaves at one health file")
        if len(leaves) != self._expected_leaves:
            raise SystemExit(
                f"the harness drove {len(leaves)} leaves, --leaves says "
                f"{self._expected_leaves}"
            )
        # The leaf set is fixed after the first cycle. Cross-cycle checks are
        # keyed by node id, so a harness that renamed its leaves every cycle
        # would make every cycle look like a first cycle -- nothing to compare
        # against, nothing to fail -- and the run would be green without a
        # single leaf ever being observed twice.
        if self.leaves and [leaf.node_id for leaf in leaves] != [
            leaf.node_id for leaf in self.leaves
        ]:
            raise SystemExit("the harness changed its leaf set mid-run")
        return leaves


def run_soak(
    *,
    executor: CycleExecutor,
    leaves_of: Callable[[], Sequence[LeafRef]],
    cycles: int | None = None,
    duration_seconds: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Drive cycles until the stopping condition, asserting health every cycle.

    Exactly one of ``cycles`` / ``duration_seconds`` selects the stopping
    condition; the per-cycle work and assertions are identical either way.
    Any failure stops the run: a soak that keeps going after the contract broke
    only produces more of the same evidence.
    """
    if (cycles is None) == (duration_seconds is None):
        raise ValueError("exactly one of cycles / duration_seconds is required")

    started = clock()
    snapshots: dict[str, LeafSnapshot] = {}
    cycle_reports: list[dict[str, Any]] = []
    failures: list[Failure] = []
    cycle = 0

    while True:
        if cycles is not None and cycle >= cycles:
            break
        if duration_seconds is not None and clock() - started >= duration_seconds:
            break
        cycle += 1
        execution = executor(cycle)
        cycle_failures = evaluate_cycle(
            cycle=cycle,
            leaves=leaves_of(),
            execution=execution,
            snapshots=snapshots,
        )
        failures.extend(cycle_failures)
        cycle_reports.append(
            {
                "cycle": cycle,
                "returncode": execution.returncode,
                "duration_seconds": execution.duration_seconds,
                "log_relative_path": execution.log_relative_path,
                "leaves_checked": len(execution.healths),
                "failures": [item.as_dict() for item in cycle_failures],
            }
        )
        if cycle_failures:
            break

    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "cycles" if cycles is not None else "duration",
        "cycles_requested": cycles,
        "duration_requested_seconds": duration_seconds,
        "cycles_completed": cycle,
        "leaves": [leaf.node_id for leaf in leaves_of()],
        "passed": not failures and cycle > 0,
        "duration_seconds": round(clock() - started, 3),
        "cycles": cycle_reports,
        "failures": [item.as_dict() for item in failures],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the three-leaf stability soak and assert leaf health."
    )
    parser.add_argument(
        "--leaves",
        type=int,
        default=DEFAULT_LEAVES,
        help="how many leaves the harness is expected to drive (default: 3)",
    )
    stop = parser.add_mutually_exclusive_group()
    stop.add_argument(
        "--cycles", type=int, help=f"stop after N cycles (default: {DEFAULT_CYCLES})"
    )
    stop.add_argument(
        "--duration-seconds",
        type=float,
        help="stop once this much wall clock has elapsed (24h lane: 86400)",
    )
    parser.add_argument("--script", type=Path, default=DEFAULT_SCRIPT)
    parser.add_argument(
        "--report", type=Path, default=Path("stability_soak_report.json")
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        help="reuse (and keep) this workspace instead of a throwaway one",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cycles = args.cycles
    duration_seconds = args.duration_seconds
    if cycles is None and duration_seconds is None:
        cycles = DEFAULT_CYCLES
    if cycles is not None and cycles < 1:
        raise SystemExit("--cycles must be at least 1")
    if duration_seconds is not None and duration_seconds <= 0:
        raise SystemExit("--duration-seconds must be positive")

    keep_workspace = args.workspace is not None
    workspace = (
        args.workspace
        if args.workspace is not None
        else Path(tempfile.mkdtemp(prefix="memory-net-soak."))
    )
    workspace.mkdir(parents=True, exist_ok=True)
    executor = HarnessCycleExecutor(
        script=args.script,
        workspace=workspace,
        expected_leaves=args.leaves,
    )
    try:
        summary = run_soak(
            executor=executor,
            leaves_of=lambda: executor.leaves,
            cycles=cycles,
            duration_seconds=duration_seconds,
        )
    finally:
        if not keep_workspace:
            shutil.rmtree(workspace, ignore_errors=True)

    summary["workspace_retained"] = keep_workspace
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                key: summary[key]
                for key in ("passed", "cycles_completed", "duration_seconds")
            },
            sort_keys=True,
        )
    )
    for failure in summary["failures"]:
        print(
            "[FAIL] cycle {cycle} leaf {node_id}: {check} {detail}".format(**failure),
            file=sys.stderr,
        )
    if not keep_workspace and not summary["passed"]:
        print(
            "re-run with --workspace <dir> to keep the leaf repos and cycle logs",
            file=sys.stderr,
        )
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
