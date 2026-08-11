"""Singleton lane and ``unclustered.json`` (DISTILL-001).

The privacy assertion here is the load-bearing one: the file lists which input
records no product cites, and it must be able to say that using ids alone. A
record body in this file would be model input landing on disk through a path the
output scanner's write-time gate does not cover.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from memory_net.distiller.llm.base import LLMProvider, LLMRequest, LLMResponse
from memory_net.distiller.orchestrator import DistillOrchestrator
from memory_net.distiller.pipeline import DistillationOutput, DistillationPipeline

SECRET_BODY = "quarterly revenue was 12 million dollars"


class OneClusterPerRecordEmbedder:
    """Puts every record in its own DBSCAN cluster, deterministically."""

    def __init__(self) -> None:
        self.policy = None

    def embed(self, texts: list[str]) -> Any:
        raise AssertionError("not used: the pipeline is given clusters directly")


class CitingProvider(LLMProvider):
    """Cites every record it is handed, so nothing lands as ``uncited``."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        records = list(request.metadata["records"])
        self.calls.append([str(record["memory_id"]) for record in records])
        payload = {
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
        return LLMResponse(
            text=json.dumps(payload),
            model="test",
            usage={"input_tokens": 5, "output_tokens": 5},
        )


class CitingNothingProvider(CitingProvider):
    """Answers a product that cites only the first record of its cluster."""

    def generate(self, request: LLMRequest) -> LLMResponse:
        response = super().generate(request)
        payload = json.loads(response.text)
        payload["products"][0]["citations"] = payload["products"][0]["citations"][:1]
        return LLMResponse(
            text=json.dumps(payload), model="test", usage=dict(response.usage)
        )


def _record(index: int, *, body: str = SECRET_BODY) -> dict[str, Any]:
    return {
        "memory_id": f"leaf-a-mem-{index}",
        "revision_hash": f"rev{index:05d}",
        "author_node": "alice",
        "source_branch_sha": "deadbeef",
        "relative_path": f"memories/auth/note-{index}.md",
        "content": f"{body} {index}",
    }


def _pipeline(tmp_path: Path, provider: LLMProvider, **kwargs: Any) -> Any:
    system = tmp_path / "system.md"
    user = tmp_path / "user.md"
    system.write_text("system", encoding="utf-8")
    user.write_text("records {{ records_json }}", encoding="utf-8")
    pipeline = DistillationPipeline(
        provider=provider,
        system_prompt_path=system,
        user_prompt_path=user,
        **kwargs,
    )
    # One record per cluster, without needing a real embedding backend: the lane
    # under test is about which clusters run and in what order.
    pipeline._cluster_records = lambda records: [  # type: ignore[method-assign]
        [record] for record in records
    ]
    return pipeline


def test_singleton_lane_off_keeps_todays_inline_order(tmp_path: Path) -> None:
    provider = CitingProvider()
    pipeline = _pipeline(tmp_path, provider)

    output = pipeline.run([_record(1), _record(2)], run_id="run")

    assert provider.calls == [["leaf-a-mem-1"], ["leaf-a-mem-2"]]
    assert output.cluster_terminal_counts == {
        "distilled": 2,
        "skipped_budget": 0,
        "skipped_parse": 0,
        "skipped_citation": 0,
        "skipped_evidence": 0,
        "noise": 0,
        "resumed": 0,
    }
    assert output.unclustered == []


def test_singleton_lane_defers_lone_records_behind_real_clusters(
    tmp_path: Path,
) -> None:
    provider = CitingProvider()
    pipeline = _pipeline(tmp_path, provider, singleton_lane_enabled=True)
    pipeline._cluster_records = lambda records: [  # type: ignore[method-assign]
        [records[0]],
        records[1:],
    ]

    pipeline.run([_record(1), _record(2), _record(3)], run_id="run")

    assert provider.calls == [
        ["leaf-a-mem-2", "leaf-a-mem-3"],
        ["leaf-a-mem-1"],
    ]


def test_singleton_lane_cap_drops_the_rest_as_noise(tmp_path: Path) -> None:
    provider = CitingProvider()
    pipeline = _pipeline(
        tmp_path,
        provider,
        singleton_lane_enabled=True,
        singleton_lane_max_records=1,
    )

    output = pipeline.run([_record(1), _record(2), _record(3)], run_id="run")

    assert provider.calls == [["leaf-a-mem-1"]]
    assert output.cluster_terminal_counts is not None
    assert output.cluster_terminal_counts["noise"] == 2
    assert [entry["reason_code"] for entry in output.unclustered or []] == [
        "noise",
        "noise",
    ]
    assert {entry["memory_id"] for entry in output.unclustered or []} == {
        "leaf-a-mem-2",
        "leaf-a-mem-3",
    }


def test_unclustered_reports_a_record_no_product_cited(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path, CitingNothingProvider())
    pipeline._cluster_records = lambda records: [records]  # type: ignore[method-assign]

    output = pipeline.run([_record(1), _record(2)], run_id="run")

    assert output.unclustered == [
        {
            "memory_id": "leaf-a-mem-2",
            "revision_hash": "rev00002",
            "reason_code": "uncited",
        }
    ]
    assert output.records_cited == 1


def test_unclustered_reports_budget_skipped_records(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path, CitingProvider(), cost_budget_usd=0.0)

    output = pipeline.run([_record(1)], run_id="run")

    assert [entry["reason_code"] for entry in output.unclustered or []] == [
        "budget_skipped"
    ]
    assert output.cluster_terminal_counts is not None
    assert output.cluster_terminal_counts["skipped_budget"] == 1


def test_unclustered_reports_parse_failure_records(tmp_path: Path) -> None:
    class BrokenProvider(LLMProvider):
        def generate(self, request: LLMRequest) -> LLMResponse:
            return LLMResponse(text="not json", model="test", usage={})

    pipeline = _pipeline(tmp_path, BrokenProvider())

    output = pipeline.run([_record(1)], run_id="run")

    assert [entry["reason_code"] for entry in output.unclustered or []] == [
        "parse_failed"
    ]


def test_unclustered_reports_evidence_rule_records(tmp_path: Path) -> None:
    pipeline = _pipeline(
        tmp_path,
        CitingProvider(),
        min_contributors_by_category={"common_knowledge": 2},
    )

    output = pipeline.run([_record(1)], run_id="run")

    assert [entry["reason_code"] for entry in output.unclustered or []] == [
        "evidence_rule_failed"
    ]


def test_unclustered_file_carries_ids_and_reasons_but_no_record_body(
    tmp_path: Path,
) -> None:
    pipeline = _pipeline(tmp_path, CitingNothingProvider())
    pipeline._cluster_records = lambda records: [records]  # type: ignore[method-assign]
    output = pipeline.run([_record(1), _record(2)], run_id="run")
    repo = tmp_path / "repo"
    repo.mkdir()
    orchestrator = DistillOrchestrator(repo_path=repo, pipeline=pipeline)

    orchestrator._write_unclustered("run-1", output)

    text = (repo / "distill_runs" / "run-1" / "unclustered.json").read_text(
        encoding="utf-8"
    )
    payload = json.loads(text)
    assert payload["run_id"] == "run-1"
    assert payload["records"] == output.unclustered
    # Allowlist: three keys, no more.
    for entry in payload["records"]:
        assert sorted(entry) == ["memory_id", "reason_code", "revision_hash"]
    # No record body, and nothing prose-shaped at all.
    assert not re.search(re.escape(SECRET_BODY), text)
    assert not re.search(r"\b(content|body|summary|evidence|text)\b", text)


def test_unclustered_file_is_written_even_when_nothing_was_dropped(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    pipeline = _pipeline(tmp_path, CitingProvider())
    orchestrator = DistillOrchestrator(repo_path=repo, pipeline=pipeline)
    output = DistillationOutput(
        products=[],
        response=LLMResponse(text="", model="test", usage={}),
        unclustered=[],
    )

    orchestrator._write_unclustered("run-2", output)

    payload = json.loads(
        (repo / "distill_runs" / "run-2" / "unclustered.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload == {"run_id": "run-2", "records": []}
