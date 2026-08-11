"""``llm.max_records_per_prompt``: the second split, and its merge (DISTILL-001).

The regression that matters most here is the *absent* key: a profile that never
mentions the split must produce the same call count and the same products it
produced before the split existed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from memory_net.distiller.cli import _max_records_per_prompt
from memory_net.distiller.llm.base import LLMProvider, LLMRequest, LLMResponse
from memory_net.distiller.pipeline import DistillationPipeline


class ChunkRecordingProvider(LLMProvider):
    """Answers one product per call, citing every record it was handed."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        records = list(request.metadata["records"])
        self.calls.append([str(record["memory_id"]) for record in records])
        payload = {
            "products": [
                {
                    "title": "Retry policy",
                    "topic_key": "retry-policy",
                    "category": "common_knowledge",
                    "summary": "Use backoff.",
                    "evidence": "Observed in production.",
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
        return LLMResponse(
            text=json.dumps(payload),
            model="test",
            usage={"input_tokens": 10, "output_tokens": 10},
        )


def _record(index: int) -> dict[str, Any]:
    return {
        "memory_id": f"leaf-a-mem-{index}",
        "revision_hash": f"rev{index:05d}",
        "author_node": f"node-{index}",
        "source_branch_sha": "deadbeef",
        "relative_path": f"memories/auth/retry-{index}.md",
        "content": f"backoff note {index}",
    }


def _pipeline(
    tmp_path: Path,
    provider: LLMProvider,
    **kwargs: Any,
) -> DistillationPipeline:
    system = tmp_path / "system.md"
    user = tmp_path / "user.md"
    system.write_text("system", encoding="utf-8")
    user.write_text("records {{ records_json }}", encoding="utf-8")
    return DistillationPipeline(
        provider=provider,
        system_prompt_path=system,
        user_prompt_path=user,
        **kwargs,
    )


def test_absent_key_keeps_one_call_per_cluster(tmp_path: Path) -> None:
    provider = ChunkRecordingProvider()
    pipeline = _pipeline(tmp_path, provider)

    output = pipeline.run([_record(1), _record(2), _record(3)], run_id="run")

    assert len(provider.calls) == 1
    assert provider.calls[0] == [
        "leaf-a-mem-1",
        "leaf-a-mem-2",
        "leaf-a-mem-3",
    ]
    assert len(output.products) == 1


def test_split_at_threshold_is_still_one_call(tmp_path: Path) -> None:
    provider = ChunkRecordingProvider()
    pipeline = _pipeline(tmp_path, provider, max_records_per_prompt=3)

    pipeline.run([_record(1), _record(2), _record(3)], run_id="run")

    assert len(provider.calls) == 1


def test_split_above_threshold_splits_into_sub_prompts(tmp_path: Path) -> None:
    provider = ChunkRecordingProvider()
    pipeline = _pipeline(tmp_path, provider, max_records_per_prompt=2)

    output = pipeline.run(
        [_record(1), _record(2), _record(3), _record(4), _record(5)],
        run_id="run",
    )

    assert [len(call) for call in provider.calls] == [2, 2, 1]
    # One cluster, one product: the sub-prompts merge before render, so the
    # duplicate-topic guard is never reached.
    assert len(output.products) == 1
    topic_ids = [product.frontmatter["topic_id"] for product in output.products]
    assert len(set(topic_ids)) == len(topic_ids)


def test_split_merges_citations_from_every_sub_prompt(tmp_path: Path) -> None:
    provider = ChunkRecordingProvider()
    pipeline = _pipeline(tmp_path, provider, max_records_per_prompt=2)
    records = [_record(index) for index in range(1, 6)]

    output = pipeline.run(records, run_id="run")

    citations = output.products[0].frontmatter["citations"]
    assert [citation["memory_id"] for citation in citations] == [
        record["memory_id"] for record in records
    ]
    # Every merged citation still resolves against the cluster snapshot.
    by_id = {record["memory_id"]: record for record in records}
    for citation in citations:
        assert (
            citation["revision_hash"] == by_id[citation["memory_id"]]["revision_hash"]
        )
        assert citation["author_node"] == by_id[citation["memory_id"]]["author_node"]


def test_split_does_not_duplicate_a_citation_seen_twice(tmp_path: Path) -> None:
    class RepeatingProvider(ChunkRecordingProvider):
        def generate(self, request: LLMRequest) -> LLMResponse:
            request.metadata["records"] = list(request.metadata["records"])
            response = super().generate(request)
            payload = json.loads(response.text)
            payload["products"][0]["citations"].append(
                payload["products"][0]["citations"][0]
            )
            return LLMResponse(
                text=json.dumps(payload),
                model=response.model,
                usage=dict(response.usage),
            )

    pipeline = _pipeline(tmp_path, RepeatingProvider(), max_records_per_prompt=1)
    records = [_record(1), _record(2)]

    output = pipeline.run(records, run_id="run")

    citations = output.products[0].frontmatter["citations"]
    assert [citation["memory_id"] for citation in citations] == [
        "leaf-a-mem-1",
        "leaf-a-mem-2",
    ]


def test_cli_reads_the_threshold_only_when_the_profile_declares_it() -> None:
    assert _max_records_per_prompt({}) is None
    assert _max_records_per_prompt({"llm": {}}) is None
    assert _max_records_per_prompt({"llm": {"max_records_per_prompt": 4}}) == 4
