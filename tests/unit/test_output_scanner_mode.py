"""Scanner mode resolution and the production lock (§5.5.9).

The negative cases are the point of this file: an output scanner that can be
downgraded from the command line or the environment is not a gate, and the only
way that stays true is to keep every downgrade route under test.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

from memory_net.distiller import cli as cli_module
from memory_net.distiller.output_scanner import (
    MODE_ENV_VAR,
    OutputScanner,
    RedactionViolation,
    Ruleset,
    ScannerConfigError,
    resolve_scanner_settings,
)
from memory_net.distiller.runctx import RunContextBuilder

PATTERNS_PATH = Path("config/redaction_patterns.yaml")
SCHEMA = "memory-net/output-scanner/v1"


def _production(**scanner: Any) -> dict[str, Any]:
    block = {"schema_version": SCHEMA, "mode": "enforce"}
    block.update(scanner)
    return {"profile_class": "production", "output_scanner": block}


def _development(**scanner: Any) -> dict[str, Any]:
    block = {"schema_version": SCHEMA, "mode": "enforce"}
    block.update(scanner)
    return {"output_scanner": block}


def _args(**overrides: Any) -> argparse.Namespace:
    values: dict[str, Any] = {
        "output_scanner_mode": None,
        "record": None,
        "record_dir": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def test_a_profile_without_the_block_falls_back_to_enforce() -> None:
    """The state a profile falls into by saying nothing has to be the gate."""
    settings = resolve_scanner_settings({}, declared_class="development")

    assert settings.mode == "enforce"
    assert settings.patterns_path == "config/redaction_patterns.yaml"


def test_a_development_profile_may_observe() -> None:
    settings = resolve_scanner_settings(
        _development(mode="observe"), declared_class="development"
    )

    assert settings.mode == "observe"


def test_the_flag_overrides_the_environment_outside_production() -> None:
    settings = resolve_scanner_settings(
        _development(),
        declared_class="development",
        cli_mode="observe",
        env_mode="enforce",
    )

    assert settings.mode == "observe"


def test_the_environment_overrides_the_profile_outside_production() -> None:
    settings = resolve_scanner_settings(
        _development(), declared_class="development", env_mode="observe"
    )

    assert settings.mode == "observe"


def test_a_missing_schema_version_is_rejected() -> None:
    with pytest.raises(ScannerConfigError, match="schema_version"):
        resolve_scanner_settings(
            {"output_scanner": {"mode": "enforce"}}, declared_class="development"
        )


def test_an_unknown_schema_version_is_rejected() -> None:
    with pytest.raises(ScannerConfigError, match="schema_version"):
        resolve_scanner_settings(
            _development(schema_version="memory-net/output-scanner/v9"),
            declared_class="development",
        )


@pytest.mark.parametrize("mode", ["audit", "warn", "block", "off", ""])
def test_leaf_side_and_invented_modes_are_rejected(mode: str) -> None:
    """The leaf side's vocabulary is a different thing acting on different
    data; accepting it here is how ``audit`` gets read as ``observe``."""
    with pytest.raises(ScannerConfigError, match="mode"):
        resolve_scanner_settings(_development(mode=mode), declared_class="development")


@pytest.mark.parametrize("path", ["/etc/patterns.yaml", "../outside.yaml"])
def test_patterns_path_must_stay_inside_the_repo(path: str) -> None:
    with pytest.raises(ScannerConfigError, match="patterns_path"):
        resolve_scanner_settings(
            _development(patterns_path=path), declared_class="development"
        )


# --------------------------------------------------------------------------
# The production lock
# --------------------------------------------------------------------------


def test_production_rejects_a_profile_that_asks_to_observe() -> None:
    """Rejected, not silently corrected: a correction nobody sees leaves the
    author believing the mode they wrote is the one in effect."""
    with pytest.raises(ScannerConfigError, match="enforce"):
        resolve_scanner_settings(
            _production(mode="observe"), declared_class="production"
        )


def test_production_rejects_the_flag() -> None:
    with pytest.raises(ScannerConfigError, match="--output-scanner-mode"):
        resolve_scanner_settings(
            _production(), declared_class="production", cli_mode="observe"
        )


def test_production_rejects_the_environment_variable() -> None:
    with pytest.raises(ScannerConfigError, match=MODE_ENV_VAR):
        resolve_scanner_settings(
            _production(), declared_class="production", env_mode="observe"
        )


def test_production_rejects_both_at_once() -> None:
    with pytest.raises(ScannerConfigError):
        resolve_scanner_settings(
            _production(),
            declared_class="production",
            cli_mode="observe",
            env_mode="observe",
        )


@pytest.mark.parametrize("value", ["enforce", "observe"])
def test_production_rejects_an_override_whatever_its_value(value: str) -> None:
    """Including the value the profile already carries. An override permitted
    when it agrees still establishes that the channel exists."""
    with pytest.raises(ScannerConfigError, match="--output-scanner-mode"):
        resolve_scanner_settings(
            _production(), declared_class="production", cli_mode=value
        )


# --------------------------------------------------------------------------
# CLI construction
# --------------------------------------------------------------------------


def test_the_context_carries_a_scanner_built_from_the_named_patterns(
    monkeypatch: Any,
) -> None:
    monkeypatch.delenv(MODE_ENV_VAR, raising=False)

    scanner = cli_module._output_scanner(
        _args(),
        _development(),
        repo_path=Path("."),
        snapshot_sha=None,
    )
    context = RunContextBuilder().set_scanner(scanner).freeze()

    assert isinstance(context.scanner, OutputScanner)
    assert context.scanner.mode == "enforce"
    # The same rule file, loaded the same way, must agree on the version.
    assert context.scanner.ruleset.version == Ruleset.from_yaml(PATTERNS_PATH).version


def test_the_local_hostname_is_injected_as_a_rule(monkeypatch: Any) -> None:
    """The machine's own name is a leak in output, and it is not in the corpus
    (writing it there would be the same leak in a tracked file)."""
    monkeypatch.delenv(MODE_ENV_VAR, raising=False)
    monkeypatch.setattr(cli_module, "read_host", lambda: "build-host-42")

    scanner = cli_module._output_scanner(
        _args(), _development(), repo_path=Path("."), snapshot_sha=None
    )

    assert scanner is not None
    with pytest.raises(RedactionViolation, match="local_hostname"):
        scanner.check("ran on build-host-42", field_kind="prose")


@pytest.mark.parametrize("flag", ["record", "record_dir"])
def test_observe_refuses_to_record(flag: str, monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.delenv(MODE_ENV_VAR, raising=False)

    with pytest.raises(SystemExit) as excinfo:
        cli_module._output_scanner(
            _args(**{flag: tmp_path / "bundle"}),
            _development(mode="observe"),
            repo_path=Path("."),
            snapshot_sha=None,
        )

    assert excinfo.value.code != 0
    assert "observe" in str(excinfo.value)


def test_production_override_exits_non_zero_from_the_cli(monkeypatch: Any) -> None:
    monkeypatch.setenv(MODE_ENV_VAR, "observe")

    with pytest.raises(SystemExit) as excinfo:
        cli_module._output_scanner(
            _args(),
            _production(),
            repo_path=Path("."),
            snapshot_sha=None,
        )

    assert excinfo.value.code != 0


def test_a_declared_but_missing_rule_file_stops_the_run(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.delenv(MODE_ENV_VAR, raising=False)

    with pytest.raises(SystemExit, match="output scanner patterns"):
        cli_module._output_scanner(
            _args(),
            _development(),
            repo_path=tmp_path,
            snapshot_sha=None,
        )


def test_no_block_and_no_rule_file_leaves_the_run_unscanned(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A repository with nothing to enforce keeps behaving as it did before the
    scanner existed. Unreachable in production, which must declare the block."""
    monkeypatch.delenv(MODE_ENV_VAR, raising=False)

    assert (
        cli_module._output_scanner(_args(), {}, repo_path=tmp_path, snapshot_sha=None)
        is None
    )
