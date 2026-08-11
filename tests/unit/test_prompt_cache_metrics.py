"""Prompt-cache accounting, from the provider's usage object to metrics.json.

The rule under test is a provenance rule, not an arithmetic one: a backend that
reports nothing produces zeros labelled ``unreported``, and the zeros are never
allowed to come from an estimate. Estimating cache hits would put a number that
looks measured next to numbers that are.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from memory_net.distiller.llm.base import LLMProvider, LLMRequest, LLMResponse
from memory_net.distiller.llm.openai_compat import _usage_of
from memory_net.distiller.metrics import DistillMetrics
from memory_net.distiller.pipeline import DistillationPipeline


class _Details:
    def __init__(self, **fields: Any) -> None:
        for name, value in fields.items():
            setattr(self, name, value)


class _Usage:
    def __init__(self, prompt: Any = 11, completion: Any = 22, **fields: Any) -> None:
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        for name, value in fields.items():
            setattr(self, name, value)


class _Completion:
    def __init__(self, usage: Any) -> None:
        self.usage = usage


def test_a_backend_that_reports_no_cache_leaves_the_keys_absent() -> None:
    usage, source = _usage_of(_Completion(_Usage()), "text")

    assert source == "reported"
    assert usage == {"input_tokens": 11, "output_tokens": 22}


def test_the_openai_shaped_details_object_is_read() -> None:
    completion = _Completion(
        _Usage(prompt_tokens_details=_Details(cached_tokens=7, cache_creation_tokens=3))
    )

    usage, _ = _usage_of(completion, "text")

    assert usage["cache_read_input_tokens"] == 7
    assert usage["cache_creation_input_tokens"] == 3


def test_flat_vendor_neutral_counters_are_read_too() -> None:
    completion = _Completion(
        _Usage(cache_read_input_tokens=5, cache_creation_input_tokens=1)
    )

    usage, _ = _usage_of(completion, "text")

    assert usage["cache_read_input_tokens"] == 5
    assert usage["cache_creation_input_tokens"] == 1


def test_a_zero_report_is_kept_and_is_not_the_same_as_silence() -> None:
    reported, _ = _usage_of(_Completion(_Usage(cache_read_input_tokens=0)), "text")
    silent, _ = _usage_of(_Completion(_Usage()), "text")

    assert reported["cache_read_input_tokens"] == 0
    assert "cache_read_input_tokens" not in silent


def test_a_malformed_cache_counter_is_treated_as_not_reported() -> None:
    completion = _Completion(
        _Usage(prompt_tokens_details=_Details(cached_tokens=-4)),
    )
    other = _Completion(_Usage(cache_read_input_tokens=True))

    assert "cache_read_input_tokens" not in _usage_of(completion, "text")[0]
    assert "cache_read_input_tokens" not in _usage_of(other, "text")[0]


def test_an_estimated_usage_never_carries_cache_counters() -> None:
    # No usage object at all: the counts are a local estimate, and a cache
    # figure attached to an estimate would be a fabrication.
    usage, source = _usage_of(_Completion(None), "12345678")

    assert source == "estimated"
    assert usage == {"input_tokens": 0, "output_tokens": 2}


def test_a_run_whose_calls_report_nothing_says_unreported(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path, _CachelessProvider())

    output = pipeline.run([_record(1)], run_id="run")

    assert output.prompt_cache_source == "unreported"
    assert output.prompt_cache_read_tokens == 0
    assert output.prompt_cache_creation_tokens == 0


def test_reported_counters_are_summed_across_calls(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path, _CachingProvider())
    pipeline._cluster_records = lambda records: [  # type: ignore[method-assign]
        [record] for record in records
    ]

    output = pipeline.run([_record(1), _record(2)], run_id="run")

    assert output.prompt_cache_source == "reported"
    assert output.prompt_cache_read_tokens == 14
    assert output.prompt_cache_creation_tokens == 6


def test_a_reported_zero_still_counts_as_a_report(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path, _CachingProvider(read=0, created=0))

    output = pipeline.run([_record(1)], run_id="run")

    assert output.prompt_cache_source == "reported"
    assert output.prompt_cache_read_tokens == 0


def test_the_two_sinks_carry_the_same_prompt_cache_values(tmp_path: Path) -> None:
    output = _pipeline(tmp_path, _CachingProvider()).run([_record(1)], run_id="run")
    metrics = DistillMetrics(
        run_id="run",
        input_records=1,
        products=1,
        prompt_cache_read_tokens=output.prompt_cache_read_tokens,
        prompt_cache_creation_tokens=output.prompt_cache_creation_tokens,
        prompt_cache_source=output.prompt_cache_source,
    ).as_dict()

    assert metrics["prompt_cache_read_tokens"] == output.prompt_cache_read_tokens
    assert (
        metrics["prompt_cache_creation_tokens"] == output.prompt_cache_creation_tokens
    )
    assert metrics["prompt_cache_source"] == "reported"


class _CachelessProvider(LLMProvider):
    def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            text=_payload(request),
            model="test",
            usage={"input_tokens": 10, "output_tokens": 10},
        )


class _CachingProvider(LLMProvider):
    def __init__(self, *, read: int = 7, created: int = 3) -> None:
        self.read = read
        self.created = created

    def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            text=_payload(request),
            model="test",
            usage={
                "input_tokens": 10,
                "output_tokens": 10,
                "cache_read_input_tokens": self.read,
                "cache_creation_input_tokens": self.created,
            },
        )


def _payload(request: LLMRequest) -> str:
    records = list(request.metadata["records"])
    return json.dumps(
        {
            "products": [
                {
                    "title": f"Topic {records[0]['memory_id']}",
                    "topic_key": str(records[0]["memory_id"]),
                    "category": "common_knowledge",
                    "summary": "Summary.",
                    "evidence": "Evidence.",
                    "citations": [
                        {
                            "memory_id": str(record["memory_id"]),
                            "revision_hash": str(record["revision_hash"]),
                        }
                        for record in records
                    ],
                }
            ]
        }
    )


def _record(index: int) -> dict[str, Any]:
    return {
        "memory_id": f"leaf-a-mem-{index}",
        "revision_hash": f"rev{index:05d}",
        "author_node": "alice",
        "source_branch_sha": "deadbeef",
        "relative_path": f"memories/note-{index}.md",
        "content": f"note {index}",
    }


def _pipeline(tmp_path: Path, provider: LLMProvider) -> Any:
    system = tmp_path / "system.md"
    user = tmp_path / "user.md"
    system.write_text("system", encoding="utf-8")
    user.write_text("records {{ records_json }}", encoding="utf-8")
    return DistillationPipeline(
        provider=provider,
        system_prompt_path=system,
        user_prompt_path=user,
    )
