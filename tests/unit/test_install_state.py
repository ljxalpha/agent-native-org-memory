"""Tests for the install state machine and its dirty-recovery evidence."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from memory_net.leaf.install_state import (
    INSTALL_STAGES,
    InstallStageError,
    failed_status,
    inconsistencies,
    read_state,
    record_dirty_recovery,
    write_state,
)

TIMESTAMP = "2026-04-20T09-00-00Z"


def test_the_stage_order_is_pinned() -> None:
    """Iterating INSTALL_STAGES cannot notice a stage going missing.

    The order is the semantics -- inconsistencies() compares positions -- so it
    is spelled out here rather than derived from the value under test.
    """
    assert INSTALL_STAGES == (
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


def test_the_install_script_records_only_known_stages_in_order() -> None:
    """A stage the script records must exist, and in the declared order."""
    script = Path("scripts/install_leaf.sh").read_text(encoding="utf-8")
    recorded = re.findall(r"record_stage ([A-Z_]+)", script)

    # An explicit sequence, not an ordered subset: "some stages, in order" stays
    # green when every stage but one is deleted.
    assert recorded == [
        "VENV_READY",
        "REPO_READY",
        "BRANCH_READY",
        "GENERATION_READY",
        "DAEMON_STARTED",
    ], recorded
    # The stages the script sets directly rather than through record_stage.
    assigned = set(re.findall(r'STAGE="([A-Z_]+)"', script))
    assert {"PREFLIGHT", "SCHEMA_PROBE", "HINTS_INSTALLED", "POST_VERIFY"} <= assigned
    assert (set(recorded) | assigned) == set(INSTALL_STAGES)


def test_every_stage_round_trips(tmp_path: Path) -> None:
    for stage in INSTALL_STAGES:
        write_state(tmp_path, stage)
        state = read_state(tmp_path)

        assert state is not None
        assert state.status == stage
        assert not state.failed
        assert state.stage == stage


def test_a_failure_records_the_stage_and_the_exit_code(tmp_path: Path) -> None:
    write_state(tmp_path, failed_status("REPO_READY"), exit_code=128, at=TIMESTAMP)

    state = read_state(tmp_path)

    assert state is not None
    assert state.failed
    assert state.stage == "REPO_READY"
    assert state.payload["exit_code"] == 128


def test_a_failure_record_carries_nothing_but_the_stage_and_code(
    tmp_path: Path,
) -> None:
    """Install commands handle a remote URL, a home path and credentials.

    This file is read by support and pasted into issues, so it must not become a
    channel for command output.
    """
    write_state(tmp_path, failed_status("BRANCH_READY"), exit_code=1, at=TIMESTAMP)

    raw = (tmp_path / "install_state.json").read_text(encoding="utf-8")

    assert set(json.loads(raw)) == {"status", "exit_code", "at"}
    forbidden = re.compile(
        r"https?://|git@|ssh://|/home/|/Users/|token|password|Traceback",
        re.IGNORECASE,
    )
    assert not forbidden.search(raw), raw


def test_a_failure_record_refuses_extra_fields(tmp_path: Path) -> None:
    """The privacy contract is worthless if any caller can bypass it."""
    with pytest.raises(InstallStageError, match="only its stage"):
        write_state(
            tmp_path,
            failed_status("REPO_READY"),
            exit_code=1,
            stderr="fatal: could not read from remote repository",
        )


def test_a_success_record_may_still_carry_context(tmp_path: Path) -> None:
    write_state(tmp_path, "REPO_READY", generation="abc")

    state = read_state(tmp_path)
    assert state is not None and state.payload["generation"] == "abc"


def test_cli_records_a_success_field(tmp_path: Path) -> None:
    """Hint injection is non-fatal, so the state must say whether it landed."""
    from memory_net.leaf import install_state

    assert (
        install_state.main(
            [
                "record",
                "--state-dir",
                str(tmp_path),
                "--status",
                "HINTS_INSTALLED",
                "--field",
                "hints-injected=false",
            ]
        )
        == 0
    )

    state = read_state(tmp_path)
    assert state is not None
    assert state.payload["hints_injected"] is False


def test_cli_refuses_a_field_on_a_failure_record(tmp_path: Path) -> None:
    from memory_net.leaf import install_state

    with pytest.raises(InstallStageError):
        install_state.main(
            [
                "record",
                "--state-dir",
                str(tmp_path),
                "--status",
                "FAILED:REPO_READY",
                "--field",
                "stderr=fatal: could not read from remote",
            ]
        )


def test_artifacts_without_readable_state_are_an_inconsistency(tmp_path: Path) -> None:
    """The most abrupt failures record nothing; they still leave a repo."""
    _home(tmp_path, venv=True, repo=True, generation=False)
    (tmp_path / "state" / "install_state.json").write_text("{oops", encoding="utf-8")

    reasons = inconsistencies(tmp_path, read_state(tmp_path / "state"))

    assert "install artifacts exist but no readable state does" in reasons


def test_a_fresh_home_is_not_an_inconsistency(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir()

    assert inconsistencies(tmp_path, read_state(tmp_path / "state")) == []


def test_a_freshly_built_venv_alone_is_not_an_inconsistency(tmp_path: Path) -> None:
    """The venv exists before recovery runs, on every clean first install.

    Counting it would fire the recovery notice every time, which trains
    operators to ignore it.
    """
    (tmp_path / "state").mkdir()
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    (tmp_path / "venv" / "bin" / "python").write_text("", encoding="utf-8")

    assert inconsistencies(tmp_path, read_state(tmp_path / "state")) == []


def test_same_second_recoveries_do_not_overwrite_each_other(tmp_path: Path) -> None:
    """Evidence that silently replaces evidence is worse than none."""
    _home(tmp_path, venv=True, repo=True, generation=True)

    first = record_dirty_recovery(tmp_path, reasons=["a"], timestamp=TIMESTAMP)
    second = record_dirty_recovery(tmp_path, reasons=["b"], timestamp=TIMESTAMP)

    assert first != second
    assert json.loads((first / "inventory.json").read_text(encoding="utf-8"))["reasons"]
    assert json.loads((second / "inventory.json").read_text(encoding="utf-8"))[
        "reasons"
    ] == ["b"]


@pytest.mark.parametrize("status", ["ALMOST_READY", "FAILED:ALMOST_READY", ""])
def test_an_unknown_stage_is_refused(tmp_path: Path, status: str) -> None:
    """A typo in the install script must not record an uninterpretable state."""
    with pytest.raises(InstallStageError):
        write_state(tmp_path, status)


def test_failed_status_refuses_an_unknown_stage() -> None:
    with pytest.raises(InstallStageError):
        failed_status("NOT_A_STAGE")


def test_the_state_is_replaced_atomically(tmp_path: Path) -> None:
    write_state(tmp_path, "VENV_READY")
    write_state(tmp_path, "REPO_READY")

    assert [path.name for path in tmp_path.glob(".*tmp")] == []
    state = read_state(tmp_path)
    assert state is not None and state.status == "REPO_READY"


@pytest.mark.parametrize("raw", ["{not json", "[]", '{"no": "status"}'])
def test_unreadable_state_reads_as_absent(tmp_path: Path, raw: str) -> None:
    (tmp_path / "install_state.json").write_text(raw, encoding="utf-8")

    assert read_state(tmp_path) is None


def test_a_clean_home_reports_no_inconsistency(tmp_path: Path) -> None:
    _home(tmp_path, venv=True, repo=True, generation=True)
    write_state(tmp_path / "state", "GENERATION_READY")

    assert inconsistencies(tmp_path, read_state(tmp_path / "state")) == []


def test_a_previous_failure_is_an_inconsistency(tmp_path: Path) -> None:
    _home(tmp_path, venv=True, repo=True, generation=True)
    write_state(tmp_path / "state", failed_status("GENERATION_READY"), exit_code=1)

    reasons = inconsistencies(tmp_path, read_state(tmp_path / "state"))

    assert reasons == ["previous install failed at stage GENERATION_READY"]


def test_state_claiming_absent_artifacts_is_an_inconsistency(tmp_path: Path) -> None:
    _home(tmp_path, venv=False, repo=False, generation=False)
    write_state(tmp_path / "state", "GENERATION_READY")

    reasons = inconsistencies(tmp_path, read_state(tmp_path / "state"))

    assert "state claims a venv that is not present" in reasons
    assert "state claims a repo that is not present" in reasons
    assert "state claims a generation that is not active" in reasons


def test_a_dangling_config_current_is_an_inconsistency(tmp_path: Path) -> None:
    _home(tmp_path, venv=True, repo=True, generation=False)
    (tmp_path / "config-current").symlink_to(tmp_path / "gone")
    write_state(tmp_path / "state", "VENV_READY")

    reasons = inconsistencies(tmp_path, read_state(tmp_path / "state"))

    assert reasons == ["config-current is a dangling symlink"]


def test_recovery_records_hashes_and_never_contents(tmp_path: Path) -> None:
    """An inventory is small enough to keep; a copy would duplicate secrets."""
    _home(tmp_path, venv=True, repo=True, generation=True)
    (tmp_path / "local.yaml").write_text(
        'node_id: "alice"\nmemory_repo: "git@example.invalid:org/team.git"\n',
        encoding="utf-8",
    )

    destination = record_dirty_recovery(
        tmp_path,
        reasons=["previous install failed at stage REPO_READY"],
        timestamp=TIMESTAMP,
    )

    raw = (destination / "inventory.json").read_text(encoding="utf-8")
    inventory = json.loads(raw)
    entries = {entry["path"]: entry for entry in inventory["entries"]}
    assert "example.invalid" not in raw
    assert "node_id" not in raw
    assert entries["local.yaml"]["kind"] == "file"
    assert len(entries["local.yaml"]["sha256"]) == 64
    assert entries["config-current"]["kind"] == "symlink"
    assert inventory["reasons"] == ["previous install failed at stage REPO_READY"]


def test_recovery_does_not_delete_or_move_anything(tmp_path: Path) -> None:
    """Recovery leaves evidence; it never silently overwrites."""
    _home(tmp_path, venv=True, repo=True, generation=True)
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))

    record_dirty_recovery(tmp_path, reasons=["dangling"], timestamp=TIMESTAMP)

    after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    assert set(before).issubset(set(after))


def _home(tmp_path: Path, *, venv: bool, repo: bool, generation: bool) -> None:
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "local.yaml").write_text("node_id: alice\n", encoding="utf-8")
    if venv:
        (tmp_path / "venv" / "bin").mkdir(parents=True)
        (tmp_path / "venv" / "bin" / "python").write_text("", encoding="utf-8")
    if repo:
        (tmp_path / "repo" / ".git").mkdir(parents=True)
    if generation:
        target = tmp_path / "config.generations" / "gen1"
        target.mkdir(parents=True)
        (tmp_path / "config-current").symlink_to(target)


def test_cli_records_a_stage_with_a_timestamp(tmp_path: Path) -> None:
    from memory_net.leaf import install_state

    assert (
        install_state.main(
            ["record", "--state-dir", str(tmp_path), "--status", "REPO_READY"]
        )
        == 0
    )

    state = read_state(tmp_path)
    assert state is not None
    assert state.status == "REPO_READY"
    assert state.payload["at"].endswith("Z")


def test_cli_recover_is_quiet_on_a_clean_home(tmp_path: Path, capsys: object) -> None:
    from memory_net.leaf import install_state

    _home(tmp_path, venv=True, repo=True, generation=True)
    write_state(tmp_path / "state", "GENERATION_READY")

    assert install_state.main(["recover", "--home", str(tmp_path)]) == 0
    assert not (tmp_path / "state" / "dirty_recovery").exists()


def test_cli_recover_records_evidence_and_still_continues(tmp_path: Path) -> None:
    """Refusing here would leave no way to repair the home, so it proceeds."""
    from memory_net.leaf import install_state

    _home(tmp_path, venv=False, repo=True, generation=True)
    write_state(tmp_path / "state", failed_status("GENERATION_READY"), exit_code=1)

    assert install_state.main(["recover", "--home", str(tmp_path)]) == 0

    inventories = list((tmp_path / "state" / "dirty_recovery").glob("*/inventory.json"))
    assert len(inventories) == 1
    inventory = json.loads(inventories[0].read_text(encoding="utf-8"))
    assert "previous install failed at stage GENERATION_READY" in inventory["reasons"]
