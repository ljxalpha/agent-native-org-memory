"""What a run's metrics say about its output scanner (§5.5.9).

Two properties are load-bearing. A reader of ``metrics.json`` must be able to
tell whether the run had a gate at all, and the observation counts must never
carry the text that tripped them -- a calibration report that quotes the secrets
it found is a leak wearing a report's clothes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from memory_net.distiller.metrics import DistillMetrics
from memory_net.distiller.orchestrator import DistillOrchestrator
from memory_net.distiller.output_scanner import OutputScanner, Ruleset
from memory_net.distiller.runctx import RunContextBuilder

PATTERNS_PATH = Path("config/redaction_patterns.yaml")
HOME_PATH = "/" + "home/" + "example-user/notes.md"


@pytest.fixture()
def ruleset() -> Ruleset:
    return Ruleset.from_yaml(PATTERNS_PATH)


def _orchestrator(tmp_path: Path, scanner: OutputScanner | None) -> DistillOrchestrator:
    context = RunContextBuilder().set_scanner(scanner).freeze()
    return DistillOrchestrator(
        repo_path=tmp_path,
        pipeline=None,  # type: ignore[arg-type]
        context=context,
    )


def test_enforce_reports_the_mode_and_the_ruleset_version(
    tmp_path: Path, ruleset: Ruleset
) -> None:
    scanner = OutputScanner(ruleset, mode="enforce")

    fields = _orchestrator(tmp_path, scanner)._output_scanner_fields()

    assert fields == {
        "output_scanner_mode": "enforce",
        "output_scanner_ruleset_version": ruleset.version,
    }


def test_observe_adds_the_distribution_without_any_content(
    tmp_path: Path, ruleset: Ruleset
) -> None:
    scanner = OutputScanner(ruleset, mode="observe")
    scanner.check(f"wrote {HOME_PATH}", field_kind="prose", field_path="summary")
    scanner.check({"note": f"and {HOME_PATH}"}, field_kind="prose")

    fields = _orchestrator(tmp_path, scanner)._output_scanner_fields()

    assert fields["output_scanner_mode"] == "observe"
    assert fields["redaction_observations"] == {"abs_home_path": {"prose": 2}}
    assert HOME_PATH not in json.dumps(fields)


def test_no_scanner_leaves_the_metric_keys_exactly_as_they_were(
    tmp_path: Path,
) -> None:
    """Regression: a run without a scanner must serialize the same key set it
    did before the scanner existed."""
    fields = _orchestrator(tmp_path, None)._output_scanner_fields()

    assert fields == {}
    written = tmp_path / "metrics.json"
    DistillMetrics(run_id="r", input_records=0, products=0).write(written)
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert "output_scanner_mode" not in payload
    assert "output_scanner_ruleset_version" not in payload
    assert "redaction_observations" not in payload


def test_present_keys_are_serialized(tmp_path: Path, ruleset: Ruleset) -> None:
    written = tmp_path / "metrics.json"
    DistillMetrics(
        run_id="r",
        input_records=0,
        products=0,
        output_scanner_mode="observe",
        output_scanner_ruleset_version=ruleset.version,
        redaction_observations={"abs_home_path": {"prose": 1}},
    ).write(written)

    payload = json.loads(written.read_text(encoding="utf-8"))

    assert payload["output_scanner_mode"] == "observe"
    assert payload["output_scanner_ruleset_version"] == ruleset.version
    assert payload["redaction_observations"] == {"abs_home_path": {"prose": 1}}


def test_the_two_sinks_agree_on_the_mode(
    tmp_path: Path, ruleset: Ruleset, monkeypatch: Any
) -> None:
    """``metrics.json`` and the bundle's ``record_metrics.json`` are read by
    different consumers and must never disagree about which gate ran."""
    scanner = OutputScanner(ruleset, mode="enforce")
    orchestrator = _orchestrator(tmp_path, scanner)
    orchestrator.record_bundle_dir = tmp_path / "bundle"
    output = _fake_output()
    monkeypatch.setattr(
        orchestrator, "_config_revision", lambda snapshot_sha: "cfg", raising=False
    )

    orchestrator._write_record_bundle(
        run_id="r", snapshot_sha="deadbeef", output=output
    )
    record_metrics = json.loads(
        (tmp_path / "bundle" / "record_metrics.json").read_text(encoding="utf-8")
    )
    run_fields = orchestrator._output_scanner_fields()

    assert record_metrics["output_scanner_mode"] == run_fields["output_scanner_mode"]
    assert (
        record_metrics["output_scanner_ruleset_version"]
        == run_fields["output_scanner_ruleset_version"]
    )


def _fake_output() -> Any:
    from memory_net.distiller.llm.base import LLMResponse
    from memory_net.distiller.pipeline import DistillationOutput

    return DistillationOutput(
        products=[],
        response=LLMResponse(text="", model="m", usage={}),
        cluster_assignments=[],
    )
