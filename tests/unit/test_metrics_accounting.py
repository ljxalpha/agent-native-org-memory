"""The two accounting identities, and the gate that enforces them.

A run whose counts do not add up has lost track of a cluster or a record, and
nothing downstream can tell whether what went missing was a skip or a product.
So the verdict is a gate, not a warning field nobody reads.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from memory_net.distiller.llm.base import LLMProvider, LLMRequest, LLMResponse
from memory_net.distiller.metrics import DistillMetrics, RunAccounting
from memory_net.distiller.orchestrator import _accounting_of, _distill_gate_error
from memory_net.distiller.pipeline import DistillationOutput, DistillationPipeline


class CitingProvider(LLMProvider):
    def generate(self, request: LLMRequest) -> LLMResponse:
        records = list(request.metadata["records"])
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
            usage={"input_tokens": 1, "output_tokens": 1},
        )


class BrokenProvider(LLMProvider):
    def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(text="not json", model="test", usage={})


def _record(index: int) -> dict[str, Any]:
    return {
        "memory_id": f"leaf-a-mem-{index}",
        "revision_hash": f"rev{index:05d}",
        "author_node": "alice",
        "source_branch_sha": "deadbeef",
        "relative_path": f"memories/note-{index}.md",
        "content": f"note {index}",
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
    pipeline._cluster_records = lambda records: [  # type: ignore[method-assign]
        [record] for record in records
    ]
    return pipeline


def _assert_balanced(output: DistillationOutput, input_records: int) -> RunAccounting:
    accounting = _accounting_of(output, input_records)
    assert accounting.clusters_total == accounting.clusters_terminal
    assert (
        accounting.records_cited + accounting.records_unclustered
        == accounting.input_records
    )
    assert accounting.mismatch is False
    return accounting


def test_a_clean_run_balances(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path, CitingProvider())
    records = [_record(1), _record(2)]

    output = pipeline.run(records, run_id="run")

    accounting = _assert_balanced(output, len(records))
    assert accounting.terminal_counts["distilled"] == 2
    assert _distill_gate_error(output, accounting=accounting) is None


def test_a_budget_exhausted_run_balances(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path, CitingProvider(), cost_budget_usd=0.0)
    records = [_record(1), _record(2)]

    output = pipeline.run(records, run_id="run")

    accounting = _assert_balanced(output, len(records))
    assert accounting.terminal_counts["skipped_budget"] == 2
    assert accounting.records_unclustered == 2


def test_a_parse_failing_run_balances(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path, BrokenProvider())
    records = [_record(1)]

    output = pipeline.run(records, run_id="run")

    accounting = _assert_balanced(output, len(records))
    assert accounting.terminal_counts["skipped_parse"] == 1
    # The parse failure is still what the gate reports first.
    error = _distill_gate_error(output, accounting=accounting)
    assert error is not None
    assert "parse failures: 1" in error
    assert "accounting mismatch" not in error


def test_a_broken_cluster_count_fails_the_gate() -> None:
    output = DistillationOutput(
        products=[],
        response=LLMResponse(text="", model="m"),
        cluster_assignments=[{"cluster_id": 0, "memory_ids": ["m-1"], "size": 1}],
        cluster_terminal_counts={"distilled": 0},
        unclustered=[
            {"memory_id": "m-1", "revision_hash": "r", "reason_code": "noise"}
        ],
        records_cited=0,
    )

    accounting = _accounting_of(output, 1)

    assert accounting.mismatch is True
    error = _distill_gate_error(output, accounting=accounting)
    assert error is not None
    assert "accounting mismatch: clusters 0/1" in error


def test_a_record_that_is_neither_cited_nor_listed_fails_the_gate() -> None:
    output = DistillationOutput(
        products=[],
        response=LLMResponse(text="", model="m"),
        cluster_assignments=[{"cluster_id": 0, "memory_ids": ["m-1"], "size": 1}],
        cluster_terminal_counts={"skipped_budget": 1},
        unclustered=[],
        records_cited=0,
    )

    accounting = _accounting_of(output, 1)

    assert accounting.mismatch is True
    assert "records 0+0/1" in (_distill_gate_error(output, accounting=accounting) or "")


def test_a_scanner_refusal_is_not_reported_as_an_accounting_fault() -> None:
    output = DistillationOutput(
        products=[],
        response=LLMResponse(text="", model="m"),
        cluster_assignments=[{"cluster_id": 0, "memory_ids": ["m-1"], "size": 1}],
        cluster_terminal_counts={"distilled": 0},
        unclustered=[],
        records_cited=0,
        redaction_failures=1,
        redaction_rule_counts={"abs_home_path": 1},
    )

    accounting = _accounting_of(output, 1)

    assert accounting.mismatch is False
    error = _distill_gate_error(output, accounting=accounting)
    assert error is not None
    assert "output scanner refusals: 1" in error
    assert "accounting mismatch" not in error


def test_an_output_that_reports_no_terminals_is_not_audited() -> None:
    output = DistillationOutput(
        products=[],
        response=LLMResponse(text="", model="m"),
        cluster_assignments=[{"cluster_id": 0, "memory_ids": ["m-1"], "size": 1}],
    )

    assert _accounting_of(output, 1).mismatch is False


def test_the_accounting_fields_serialize_into_metrics() -> None:
    accounting = RunAccounting.of(
        clusters_total=2,
        terminal_counts={"distilled": 1, "noise": 1},
        records_cited=1,
        records_unclustered=1,
        input_records=2,
    )

    metrics = DistillMetrics(
        run_id="run",
        input_records=2,
        products=1,
        **accounting.as_metrics_fields(),  # type: ignore[arg-type]
    ).as_dict()

    assert metrics["clusters_total"] == 2
    assert metrics["clusters_terminal"] == 2
    assert metrics["cluster_terminal_counts"] == {"distilled": 1, "noise": 1}
    assert metrics["records_cited"] == 1
    assert metrics["records_unclustered"] == 1
    assert metrics["accounting_mismatch"] is False
    assert metrics["accounting_audited"] is True
    # clusters_attempted keeps its own, older meaning and is untouched by this.
    assert metrics["clusters_attempted"] == 0


def test_an_aborted_run_reports_that_it_did_not_audit_the_counts() -> None:
    """Not audited and balanced must not look alike.

    A scanner abort leaves clusters with no terminal by design, so no verdict
    can be reached -- but reporting accounting_mismatch: false is the same
    provenance mistake as reporting an estimate as a measurement.
    """
    accounting = RunAccounting.of(
        clusters_total=3,
        terminal_counts={"distilled": 1},
        records_cited=1,
        records_unclustered=0,
        input_records=3,
        aborted=True,
    )

    fields = accounting.as_metrics_fields()

    assert fields["accounting_mismatch"] is False
    assert fields["accounting_audited"] is False


def test_a_run_that_reported_no_terminals_was_not_audited_either() -> None:
    accounting = RunAccounting.of(
        clusters_total=2,
        terminal_counts=None,
        records_cited=0,
        records_unclustered=0,
        input_records=2,
    )

    assert accounting.mismatch is False
    assert accounting.audited is False
