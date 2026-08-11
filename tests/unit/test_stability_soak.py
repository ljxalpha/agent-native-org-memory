"""Tests for the three-leaf stability soak driver.

The cycle executor is injected, so every assertion below is exercised against
crafted health payloads rather than against a live harness run. The live run is
the CI step (``--cycles 5``) and the opt-in end-to-end test at the bottom.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts.stability_soak import (
    CycleExecution,
    HarnessCycleExecutor,
    HealthRead,
    LeafRef,
    evaluate_leaf,
    main,
    read_health,
    run_soak,
    sanitize_detail,
)

LEAF_IDS = ("alice-claude-01", "bob-claude-01", "carol-claude-01")
LEAVES = tuple(
    LeafRef(node_id=node_id, health_path=Path(f"/nonexistent/{node_id}.json"))
    for node_id in LEAF_IDS
)
#: A sentence in the shape of a reported memory. Nothing like it may reach the
#: report, no matter which field it was injected through.
MEMORY_SENTENCE = "When OAuth token refresh returns 429, retry with backoff."


def health_payload(node_id: str, cycle: int, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "memory-net/health/v1",
        "node_id": node_id,
        "role": "leaf",
        "status": "healthy",
        "last_heartbeat_at": f"2026-08-10T00:0{cycle}:00Z",
        "published_at": f"2026-08-10T00:0{cycle}:00Z",
        "last_report_at": f"2026-08-10T00:0{cycle}:00Z",
        "recent_errors": [],
        "redaction_incidents": {"blocked_reports": 0, "scanner_hits": {}},
        "memory_count": cycle,
        "adapter": "claude-code",
        "install_id": "i" * 32,
        "code_version": "0.1.0",
        "host": "runner",
        "build_id": "0.1.0",
        "config_source_master_sha": "a" * 40,
        "started_at": "2026-08-10T00:00:00Z",
    }
    payload.update(overrides)
    return payload


def as_read(payload: dict[str, Any]) -> HealthRead:
    raw = json.dumps(payload, sort_keys=True)
    return HealthRead(payload=payload, digest=f"digest-{hash(raw) & 0xFFFFFFFF:x}")


class FakeExecutor:
    """Replays a scripted health payload per leaf per cycle."""

    def __init__(
        self,
        cycles: list[dict[str, HealthRead]],
        returncodes: dict[int, int] | None = None,
    ) -> None:
        self._cycles = cycles
        self._returncodes = returncodes or {}
        self.calls: list[int] = []

    def __call__(self, cycle: int) -> CycleExecution:
        self.calls.append(cycle)
        returncode = self._returncodes.get(cycle, 0)
        if returncode != 0:
            return CycleExecution(
                returncode=returncode,
                duration_seconds=0.1,
                log_relative_path=f"logs/cycle-{cycle}.log",
            )
        healths = self._cycles[(cycle - 1) % len(self._cycles)]
        return CycleExecution(
            returncode=0,
            duration_seconds=0.1,
            log_relative_path=f"logs/cycle-{cycle}.log",
            healths=healths,
        )


def green_cycles(count: int) -> list[dict[str, HealthRead]]:
    return [
        {node_id: as_read(health_payload(node_id, cycle)) for node_id in LEAF_IDS}
        for cycle in range(1, count + 1)
    ]


def soak(executor: FakeExecutor, **kwargs: Any) -> dict[str, Any]:
    return run_soak(executor=executor, leaves_of=lambda: LEAVES, **kwargs)


def checks(summary: dict[str, Any]) -> list[tuple[str, str]]:
    return [(item["node_id"], item["check"]) for item in summary["failures"]]


def test_two_green_cycles_pass_and_check_every_leaf() -> None:
    executor = FakeExecutor(green_cycles(2))

    summary = soak(executor, cycles=2)

    assert summary["passed"] is True
    assert summary["cycles_completed"] == 2
    assert summary["failures"] == []
    assert executor.calls == [1, 2]
    assert [cycle["leaves_checked"] for cycle in summary["cycles"]] == [3, 3]
    assert summary["leaves"] == list(LEAF_IDS)


def test_degraded_leaf_fails_and_names_that_leaf() -> None:
    cycles = green_cycles(2)
    cycles[1]["bob-claude-01"] = as_read(
        health_payload("bob-claude-01", 2, status="degraded")
    )

    summary = soak(FakeExecutor(cycles), cycles=2)

    assert summary["passed"] is False
    assert ("bob-claude-01", "status") in checks(summary)
    assert [node_id for node_id, _ in checks(summary)] == ["bob-claude-01"]
    assert summary["failures"][0]["cycle"] == 2


def test_last_report_at_regression_fails() -> None:
    cycles = green_cycles(2)
    cycles[1]["carol-claude-01"] = as_read(
        health_payload("carol-claude-01", 2, last_report_at="2026-08-09T23:00:00Z")
    )

    summary = soak(FakeExecutor(cycles), cycles=2)

    assert ("carol-claude-01", "last_report_at_regressed") in checks(summary)


def test_equal_last_report_at_is_not_a_regression() -> None:
    cycles = green_cycles(2)
    # Two cycles inside one second: the timestamp repeats, the payload does not.
    cycles[1]["alice-claude-01"] = as_read(
        health_payload("alice-claude-01", 2, last_report_at="2026-08-10T00:01:00Z")
    )

    summary = soak(FakeExecutor(cycles), cycles=2)

    assert summary["passed"] is True


def test_unchanged_health_file_fails_as_not_regenerated() -> None:
    frozen = {node_id: as_read(health_payload(node_id, 1)) for node_id in LEAF_IDS}

    summary = soak(FakeExecutor([frozen, frozen]), cycles=2)

    assert summary["passed"] is False
    assert {check for _, check in checks(summary)} == {"health_not_regenerated"}


def test_growing_blocked_reports_fails() -> None:
    cycles = green_cycles(2)
    cycles[1]["alice-claude-01"] = as_read(
        health_payload(
            "alice-claude-01",
            2,
            redaction_incidents={"blocked_reports": 1, "scanner_hits": {}},
        )
    )

    summary = soak(FakeExecutor(cycles), cycles=2)

    assert ("alice-claude-01", "blocked_reports_increased") in checks(summary)


def test_stable_blocked_reports_pass() -> None:
    cycles = [
        {
            node_id: as_read(
                health_payload(
                    node_id,
                    cycle,
                    redaction_incidents={"blocked_reports": 2, "scanner_hits": {}},
                )
            )
            for node_id in LEAF_IDS
        }
        for cycle in (1, 2)
    ]

    summary = soak(FakeExecutor(cycles), cycles=2)

    assert summary["passed"] is True


def test_missing_operational_identity_fails_the_strict_check() -> None:
    payload = health_payload("alice-claude-01", 1)
    del payload["install_id"]
    cycles = green_cycles(1)
    cycles[0]["alice-claude-01"] = as_read(payload)

    summary = soak(FakeExecutor(cycles), cycles=1)

    assert ("alice-claude-01", "health_schema") in checks(summary)
    assert "install_id" in summary["failures"][0]["detail"]


def test_recent_errors_are_counted_never_quoted() -> None:
    cycles = green_cycles(1)
    cycles[0]["bob-claude-01"] = as_read(
        health_payload("bob-claude-01", 1, recent_errors=[MEMORY_SENTENCE])
    )

    summary = soak(FakeExecutor(cycles), cycles=1)

    assert ("bob-claude-01", "recent_errors") in checks(summary)
    assert summary["failures"][0]["detail"] == "count=1"
    assert MEMORY_SENTENCE not in json.dumps(summary)


def test_report_never_carries_memory_text() -> None:
    payload = health_payload(
        "carol-claude-01",
        1,
        status=MEMORY_SENTENCE,
        recent_errors=[MEMORY_SENTENCE, "alice@example.com"],
    )
    cycles = green_cycles(1)
    cycles[0]["carol-claude-01"] = as_read(payload)

    serialized = json.dumps(soak(FakeExecutor(cycles), cycles=1))

    for forbidden in ("OAuth", "429", "retry with backoff", "alice@example.com"):
        assert forbidden not in serialized


def test_harness_failure_stops_the_run() -> None:
    executor = FakeExecutor(green_cycles(3), returncodes={2: 3})

    summary = soak(executor, cycles=3)

    assert summary["passed"] is False
    assert executor.calls == [1, 2]
    assert checks(summary) == [("-", "harness_failed")]
    assert summary["failures"][0]["detail"] == "returncode=3"


def test_unreadable_health_fails_without_resetting_the_baseline() -> None:
    failures, snapshot = evaluate_leaf(
        cycle=2,
        node_id="alice-claude-01",
        read=HealthRead(payload=None, digest="", error="invalid JSON: JSONDecodeError"),
        previous=None,
    )

    assert snapshot is None
    assert [item.check for item in failures] == ["health_unreadable"]


def test_duration_mode_stops_on_the_clock() -> None:
    ticks = iter([0.0, 0.0, 3.0, 6.0, 9.0, 12.0, 15.0])
    executor = FakeExecutor(green_cycles(10))

    summary = run_soak(
        executor=executor,
        leaves_of=lambda: LEAVES,
        duration_seconds=7.0,
        clock=lambda: next(ticks),
    )

    assert summary["mode"] == "duration"
    assert summary["cycles_completed"] == 3
    assert summary["passed"] is True


def test_exactly_one_stopping_condition_is_required() -> None:
    executor = FakeExecutor(green_cycles(1))

    with pytest.raises(ValueError):
        soak(executor, cycles=1, duration_seconds=1.0)
    with pytest.raises(ValueError):
        soak(executor)


def test_sanitize_detail_bounds_and_filters() -> None:
    assert sanitize_detail("unsupported health status=  'degraded'\n") == (
        "unsupported health status=<redacted>"
    )
    assert sanitize_detail("missing fields: adapter, install_id") == (
        "missing fields: adapter, install_id"
    )
    assert len(sanitize_detail("x" * 500)) == 163


def test_read_health_digests_the_bytes(tmp_path: Path) -> None:
    path = tmp_path / ".health.json"
    path.write_text(json.dumps(health_payload("alice-claude-01", 1)), encoding="utf-8")
    first = read_health(path)
    path.write_text(json.dumps(health_payload("alice-claude-01", 2)), encoding="utf-8")
    second = read_health(path)

    assert first.error is None
    assert first.digest != second.digest


def test_read_health_reports_a_missing_file(tmp_path: Path) -> None:
    read = read_health(tmp_path / "absent.json")

    assert read.payload is None
    assert read.error is not None


def test_executor_rejects_a_leaf_count_mismatch(tmp_path: Path) -> None:
    (tmp_path / "leaves.json").write_text(
        json.dumps(
            {"leaves": [{"node_id": "alice-claude-01", "health_path": "/tmp/a.json"}]}
        ),
        encoding="utf-8",
    )

    def runner(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=[], returncode=0)

    executor = HarnessCycleExecutor(
        script=Path("unused.sh"),
        workspace=tmp_path,
        expected_leaves=3,
        runner=runner,
    )

    with pytest.raises(SystemExit):
        executor(1)


def test_executor_keeps_harness_output_out_of_the_result(tmp_path: Path) -> None:
    def runner(*_args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        kwargs["stdout"].write(MEMORY_SENTENCE.encode("utf-8"))
        return subprocess.CompletedProcess(args=[], returncode=7)

    executor = HarnessCycleExecutor(
        script=Path("unused.sh"),
        workspace=tmp_path,
        expected_leaves=3,
        runner=runner,
    )
    execution = executor(1)

    assert execution.returncode == 7
    assert execution.healths == {}
    assert execution.log_relative_path == "logs/cycle-1.log"
    assert MEMORY_SENTENCE in (tmp_path / "logs" / "cycle-1.log").read_text(
        encoding="utf-8"
    )


@pytest.mark.skipif(
    os.environ.get("MEMORY_NET_SOAK_E2E") != "1",
    reason="set MEMORY_NET_SOAK_E2E=1 to run the live two-cycle soak",
)
def test_live_two_cycle_soak_is_green(tmp_path: Path) -> None:
    report = tmp_path / "soak.json"

    assert main(["--leaves", "3", "--cycles", "2", "--report", str(report)]) == 0

    summary = json.loads(report.read_text(encoding="utf-8"))
    assert summary["passed"] is True
    assert summary["cycles_completed"] == 2
    assert sorted(summary["leaves"]) == sorted(LEAF_IDS)
