"""Run-level provenance aggregation (§9.5 of the design doc).

A run mixes its primary and fallback lanes, so "where did this run's token
accounting come from" needs a rule fixed up front, otherwise the answer is
undefined exactly in the mixed case.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from memory_net.distiller.llm.base import LLMProvider, LLMRequest, LLMResponse
from memory_net.distiller.llm.mock import MockLLMProvider
from memory_net.distiller.pipeline import DistillationPipeline
from memory_net.distiller.provenance import RunProvenance, aggregate


def _response(usage_source: str, provider_backend: str) -> LLMResponse:
    return LLMResponse(
        text="{}",
        model="m",
        usage_source=usage_source,
        provider_backend=provider_backend,
    )


def test_zero_calls_is_none_with_empty_backend_list() -> None:
    result = aggregate([])
    assert result.usage_source == "none"
    assert result.provider_backends == []
    assert result.usage_source_counts == {"reported": 0, "estimated": 0, "unknown": 0}


def test_empty_matches_aggregate_of_no_calls() -> None:
    assert RunProvenance.empty() == aggregate([])


def test_all_reported_is_reported() -> None:
    result = aggregate([_response("reported", "deep"), _response("reported", "deep")])
    assert result.usage_source == "reported"
    assert result.provider_backends == ["deep"]
    assert result.usage_source_counts == {"reported": 2, "estimated": 0, "unknown": 0}


@pytest.mark.parametrize("value", ["estimated", "unknown"])
def test_a_single_non_reported_value_is_carried_through(value: str) -> None:
    result = aggregate([_response(value, "b"), _response(value, "b")])
    assert result.usage_source == value
    assert result.usage_source_counts[value] == 2


def test_two_distinct_values_are_mixed_with_counts() -> None:
    """Primary reported + fallback estimated is the canonical mixed run."""
    result = aggregate(
        [
            _response("reported", "primary"),
            _response("estimated", "fallback"),
            _response("estimated", "fallback"),
        ]
    )
    assert result.usage_source == "mixed"
    assert result.usage_source_counts == {"reported": 1, "estimated": 2, "unknown": 0}


def test_backends_are_deduplicated_and_sorted() -> None:
    result = aggregate(
        [
            _response("reported", "zeta"),
            _response("reported", "alpha"),
            _response("reported", "zeta"),
        ]
    )
    assert result.provider_backends == ["alpha", "zeta"]


def test_backend_order_does_not_depend_on_call_order() -> None:
    forward = aggregate([_response("reported", "a"), _response("reported", "b")])
    backward = aggregate([_response("reported", "b"), _response("reported", "a")])
    assert forward.provider_backends == backward.provider_backends


def test_scalar_backend_is_not_offered() -> None:
    """A scalar could only encode a mixed run as "mixed" — lossy and misleading."""
    assert not hasattr(aggregate([]), "provider_backend")


class _BrokenJSONProvider(LLMProvider):
    """Primary lane that never parses, forcing every cluster to the fallback."""

    def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            text="not json at all",
            model=request.model,
            usage={"input_tokens": 1, "output_tokens": 1},
            usage_source="reported",
            provider_backend="broken",
        )


def test_pipeline_collects_primary_lane_provenance(tmp_path: Path) -> None:
    output = _run_pipeline(tmp_path, provider=MockLLMProvider())
    assert output.products
    assert output.provenance.usage_source == "estimated"
    assert output.provenance.provider_backends == ["mock"]
    assert output.provenance.usage_source_counts["estimated"] == 1


def test_pipeline_collects_both_lanes(tmp_path: Path) -> None:
    output = _run_pipeline(
        tmp_path,
        provider=_BrokenJSONProvider(),
        fallback_provider=MockLLMProvider(),
    )
    assert output.fallback_attempts == 1
    assert output.provenance.provider_backends == ["broken", "mock"]
    assert output.provenance.usage_source == "mixed"
    assert output.provenance.usage_source_counts == {
        "reported": 1,
        "estimated": 1,
        "unknown": 0,
    }


def test_pipeline_with_no_records_reports_none(tmp_path: Path) -> None:
    output = _run_pipeline(tmp_path, provider=MockLLMProvider(), records=[])
    assert output.provenance == RunProvenance.empty()


def _run_pipeline(
    tmp_path: Path,
    *,
    provider: LLMProvider,
    fallback_provider: LLMProvider | None = None,
    records: list[dict[str, Any]] | None = None,
) -> Any:
    system_prompt = tmp_path / "system.md"
    user_prompt = tmp_path / "user.md"
    system_prompt.write_text("system", encoding="utf-8")
    user_prompt.write_text("records {{ records_json }}", encoding="utf-8")
    pipeline = DistillationPipeline(
        provider=provider,
        fallback_provider=fallback_provider,
        system_prompt_path=system_prompt,
        user_prompt_path=user_prompt,
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
