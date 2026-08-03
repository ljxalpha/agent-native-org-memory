"""Run provenance reaches both metrics sinks with identical values (§9.5).

``distill_runs/<run_id>/metrics.json`` and the bundle's ``record_metrics.json``
are read by different consumers; if they could disagree, "where did this run's
token accounting come from" would have two answers.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from memory_net.distiller.llm.base import LLMProvider, LLMRequest, LLMResponse
from memory_net.distiller.llm.mock import MockLLMProvider
from memory_net.distiller.metrics import DistillMetrics
from memory_net.distiller.orchestrator import DistillOrchestrator
from memory_net.distiller.pipeline import DistillationOutput, DistillationPipeline

PROVENANCE_KEYS = ("provider_backends", "usage_source", "usage_source_counts")
EMPTY_COUNTS = {"reported": 0, "estimated": 0, "unknown": 0}


class _ReportedProvider(LLMProvider):
    """Stands in for a backend that returns its own token counts."""

    def generate(self, request: LLMRequest) -> LLMResponse:
        return dataclasses.replace(
            MockLLMProvider().generate(request),
            usage={"input_tokens": 7, "output_tokens": 9},
            usage_source="reported",
            provider_backend="reported-backend",
        )


class _UnparsableProvider(LLMProvider):
    """Primary lane that never parses, so every cluster falls back."""

    def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            text="no json here",
            model=request.model,
            usage={"input_tokens": 1, "output_tokens": 1},
            usage_source="reported",
            provider_backend="broken",
        )


def test_metrics_defaults_describe_a_zero_call_run() -> None:
    metrics = DistillMetrics(run_id="r", input_records=0, products=0)
    assert metrics.provider_backends == []
    assert metrics.usage_source == "none"
    assert metrics.usage_source_counts == EMPTY_COUNTS


def test_mock_run_is_reported_as_estimated(tmp_path: Path) -> None:
    metrics, record_metrics = _write(tmp_path, _distill(tmp_path, MockLLMProvider()))
    assert metrics["usage_source"] == "estimated"
    assert metrics["provider_backends"] == ["mock"]
    assert metrics["usage_source_counts"]["estimated"] == 1
    _assert_sinks_agree(metrics, record_metrics)


def test_backend_reported_usage_survives_to_both_sinks(tmp_path: Path) -> None:
    metrics, record_metrics = _write(tmp_path, _distill(tmp_path, _ReportedProvider()))
    assert metrics["usage_source"] == "reported"
    assert metrics["provider_backends"] == ["reported-backend"]
    _assert_sinks_agree(metrics, record_metrics)


def test_mixed_lanes_are_reported_as_mixed_with_counts(tmp_path: Path) -> None:
    output = _distill(
        tmp_path, _UnparsableProvider(), fallback_provider=MockLLMProvider()
    )
    metrics, record_metrics = _write(tmp_path, output)
    assert metrics["usage_source"] == "mixed"
    assert metrics["provider_backends"] == ["broken", "mock"]
    assert metrics["usage_source_counts"] == {
        "reported": 1,
        "estimated": 1,
        "unknown": 0,
    }
    _assert_sinks_agree(metrics, record_metrics)


def test_zero_call_run_is_reported_as_none(tmp_path: Path) -> None:
    output = _distill(tmp_path, MockLLMProvider(), records=[])
    metrics, record_metrics = _write(tmp_path, output)
    assert metrics["usage_source"] == "none"
    assert metrics["provider_backends"] == []
    assert metrics["usage_source_counts"] == EMPTY_COUNTS
    _assert_sinks_agree(metrics, record_metrics)


def test_failure_metrics_carry_provenance_when_an_output_exists(tmp_path: Path) -> None:
    output = _distill(tmp_path, MockLLMProvider())
    orchestrator = _orchestrator(tmp_path)
    orchestrator._write_failure_outputs(
        run_id="run-1",
        snapshot_sha="0" * 40,
        records=[],
        error="boom",
        duration_seconds=0.0,
        output=output,
    )
    metrics = _read(tmp_path / "distill_runs" / "run-1" / "metrics.json")
    assert metrics["usage_source"] == "estimated"
    assert metrics["provider_backends"] == ["mock"]


def test_failure_metrics_without_an_output_report_none(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    orchestrator._write_failure_outputs(
        run_id="run-1",
        snapshot_sha="0" * 40,
        records=[],
        error="boom",
        duration_seconds=0.0,
    )
    metrics = _read(tmp_path / "distill_runs" / "run-1" / "metrics.json")
    assert metrics["usage_source"] == "none"
    assert metrics["provider_backends"] == []
    assert metrics["usage_source_counts"] == EMPTY_COUNTS


def _assert_sinks_agree(
    metrics: dict[str, Any], record_metrics: dict[str, Any]
) -> None:
    for key in PROVENANCE_KEYS:
        assert key in record_metrics, key
        assert metrics[key] == record_metrics[key], key


def _distill(
    tmp_path: Path,
    provider: LLMProvider,
    *,
    fallback_provider: LLMProvider | None = None,
    records: list[dict[str, Any]] | None = None,
) -> DistillationOutput:
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    (prompts / "distill_system.md").write_text("system", encoding="utf-8")
    (prompts / "distill_user.md").write_text(
        "records {{ records_json }}", encoding="utf-8"
    )
    pipeline = DistillationPipeline(
        provider=provider,
        fallback_provider=fallback_provider,
        system_prompt_path=prompts / "distill_system.md",
        user_prompt_path=prompts / "distill_user.md",
        categories=["auth"],
    )
    if records is None:
        records = [
            {
                "memory_id": "leaf-a-mem-1",
                "revision_hash": "abc12345",
                "author_node": "alice",
                "source_branch_sha": "deadbeef",
                "relative_path": "memories/auth/retry-abc12345.md",
                "category_hint": "auth",
                "content": "---\ntitle: Retry\n---\nUse backoff.",
            }
        ]
    return pipeline.run(records, run_id="run-1")


def _orchestrator(tmp_path: Path) -> DistillOrchestrator:
    return DistillOrchestrator(
        repo_path=tmp_path,
        pipeline=_distill_pipeline(tmp_path),
        no_push=True,
        record_bundle_dir=tmp_path / "record_runs" / "bundle",
    )


def _distill_pipeline(tmp_path: Path) -> DistillationPipeline:
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    (prompts / "distill_system.md").write_text("system", encoding="utf-8")
    (prompts / "distill_user.md").write_text(
        "records {{ records_json }}", encoding="utf-8"
    )
    return DistillationPipeline(
        provider=MockLLMProvider(),
        system_prompt_path=prompts / "distill_system.md",
        user_prompt_path=prompts / "distill_user.md",
    )


def _write(
    tmp_path: Path, output: DistillationOutput
) -> tuple[dict[str, Any], dict[str, Any]]:
    orchestrator = _orchestrator(tmp_path)
    snapshot_sha = "0" * 40
    orchestrator._write_record_bundle(
        run_id="run-1", snapshot_sha=snapshot_sha, output=output
    )
    orchestrator._distill_metrics(
        run_id="run-1",
        snapshot_sha=snapshot_sha,
        records=[],
        output=output,
        duration_seconds=0.0,
    ).write(tmp_path / "distill_runs" / "run-1" / "metrics.json")
    return (
        _read(tmp_path / "distill_runs" / "run-1" / "metrics.json"),
        _read(tmp_path / "record_runs" / "bundle" / "record_metrics.json"),
    )


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value
