"""Soft and per-cluster ceilings, as the pipeline and the CLI see them.

The four profile combinations are asserted here because the failure they guard
against is silent: a soft ceiling accepted without a hard one would look like a
budget while capping nothing the run actually bills against.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from memory_net.distiller.cli import _run_budget
from memory_net.distiller.llm.base import LLMProvider, LLMRequest, LLMResponse
from memory_net.distiller.pipeline import DistillationPipeline
from memory_net.distiller.runctx import RunBudget, RunContext, prompt_tokens_upper


class ReservingProvider(LLMProvider):
    """A lane that reserves before it answers, like the production backend."""

    def __init__(self, budget: RunBudget) -> None:
        self.budget = budget
        self.calls: list[str] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        reservation = self.budget.reserve(
            input_tokens_upper=prompt_tokens_upper(
                request.system_prompt, request.user_prompt
            ),
            output_tokens_upper=request.max_tokens,
            timeout_s_upper=1.0,
        )
        records = list(request.metadata["records"])
        self.calls.append(str(records[0]["memory_id"]))
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
        usage = {"input_tokens": 1_000, "output_tokens": 1_000}
        reservation.settle(usage=usage, wall_s=0.0)
        return LLMResponse(text=json.dumps(payload), model="test", usage=usage)


def _record(index: int) -> dict[str, Any]:
    return {
        "memory_id": f"leaf-a-mem-{index}",
        "revision_hash": f"rev{index:05d}",
        "author_node": "alice",
        "source_branch_sha": "deadbeef",
        "relative_path": f"memories/note-{index}.md",
        "content": f"note {index}",
    }


def _pipeline(tmp_path: Path, budget: RunBudget) -> DistillationPipeline:
    system = tmp_path / "system.md"
    user = tmp_path / "user.md"
    system.write_text("system", encoding="utf-8")
    user.write_text("records {{ records_json }}", encoding="utf-8")
    pipeline = DistillationPipeline(
        provider=ReservingProvider(budget),
        system_prompt_path=system,
        user_prompt_path=user,
        context=RunContext(budget=budget),
    )
    pipeline._cluster_records = lambda records: [  # type: ignore[method-assign]
        [record] for record in records
    ]
    return pipeline


def test_soft_ceiling_stops_opening_clusters_but_finishes_the_open_one(
    tmp_path: Path,
) -> None:
    # One call costs 2e-3 USD, so the soft ceiling falls after the first.
    budget = RunBudget(
        input_per_mtok_usd=1.0,
        output_per_mtok_usd=1.0,
        max_usd=1.0,
        soft_max_usd=0.002,
    )
    pipeline = _pipeline(tmp_path, budget)

    output = pipeline.run([_record(1), _record(2), _record(3)], run_id="run")

    provider = pipeline.provider
    assert isinstance(provider, ReservingProvider)
    assert provider.calls == ["leaf-a-mem-1"]
    assert len(output.products) == 1
    assert output.skipped_clusters == 2
    assert output.cluster_terminal_counts is not None
    assert output.cluster_terminal_counts["skipped_budget"] == 2
    assert budget.soft_exhausted is True


def test_per_cluster_ceiling_skips_only_that_cluster(tmp_path: Path) -> None:
    # Each cluster gets one call's worth and no more; a cluster needing two
    # loses only itself.
    budget = RunBudget(
        input_per_mtok_usd=1.0,
        output_per_mtok_usd=1.0,
        max_usd=1.0,
        per_cluster_max_usd=0.5,
    )
    pipeline = _pipeline(tmp_path, budget)
    pipeline.max_records_per_prompt = 1
    pipeline._cluster_records = lambda records: [  # type: ignore[method-assign]
        records[:2],
        records[2:],
    ]
    # A ceiling that admits the first sub-prompt of a cluster but not the
    # second: 2e-3 spent, upper bound of the next reserve is far above the rest.
    budget.per_cluster_max_usd = 0.002_5

    output = pipeline.run([_record(1), _record(2), _record(3)], run_id="run")

    provider = pipeline.provider
    assert isinstance(provider, ReservingProvider)
    # First cluster: sub-prompt one runs, sub-prompt two is refused. Second
    # cluster still runs -- the run did not stop.
    assert provider.calls == ["leaf-a-mem-1", "leaf-a-mem-3"]
    # The refused cluster publishes nothing: what its first sub-prompt returned
    # would be a product citing half its cluster, indistinguishable from one
    # that saw all of it.
    assert len(output.products) == 1
    assert output.cluster_terminal_counts is not None
    assert output.cluster_terminal_counts["skipped_budget"] == 1
    assert output.cluster_terminal_counts["distilled"] == 1
    assert budget.spent_usd == pytest.approx(4e-3)


def test_the_two_lanes_share_one_scope_and_do_not_double_spend(
    tmp_path: Path,
) -> None:
    budget = RunBudget(
        input_per_mtok_usd=1.0,
        output_per_mtok_usd=1.0,
        max_usd=1.0,
        per_cluster_max_usd=0.5,
    )
    pipeline = _pipeline(tmp_path, budget)
    pipeline.fallback_provider = pipeline.provider

    pipeline.run([_record(1)], run_id="run")

    assert budget.attempts == 1
    assert budget.scope_spent("cluster-0") == pytest.approx(budget.spent_usd)


def test_only_the_flat_key_gives_todays_budget() -> None:
    budget = _run_budget({"llm": {"budget_usd": 5.0}})

    assert budget is not None
    assert budget.max_usd == 5.0
    assert budget.soft_max_usd is None
    assert budget.per_cluster_max_usd is None


def test_the_sub_block_alone_is_refused() -> None:
    with pytest.raises(SystemExit, match="needs a hard ceiling"):
        _run_budget({"llm": {"budget": {"soft_max_usd": 1.0}}})


def test_both_together_are_accepted_and_the_flat_key_still_wins() -> None:
    budget = _run_budget(
        {
            "llm": {
                "budget_usd": 5.0,
                "budget": {"soft_max_usd": 4.0, "per_cluster_max_usd": 1.0},
            }
        }
    )

    assert budget is not None
    assert (budget.max_usd, budget.soft_max_usd, budget.per_cluster_max_usd) == (
        5.0,
        4.0,
        1.0,
    )


def test_the_sub_block_may_narrow_a_legacy_cost_budget() -> None:
    budget = _run_budget(
        {
            "cost_budget": {"budget_usd": 5.0},
            "llm": {"budget": {"per_cluster_max_usd": 1.0}},
        }
    )

    assert budget is not None
    assert budget.max_usd == 5.0
    assert budget.per_cluster_max_usd == 1.0


def test_a_looser_sub_block_is_refused_rather_than_clamped() -> None:
    with pytest.raises(SystemExit, match="must not exceed max_usd"):
        _run_budget({"llm": {"budget_usd": 1.0, "budget": {"soft_max_usd": 2.0}}})


@pytest.mark.parametrize("value", ["1.0", True, []])
def test_a_non_numeric_ceiling_is_refused(value: object) -> None:
    with pytest.raises(SystemExit, match="must be a number"):
        _run_budget({"llm": {"budget_usd": 1.0, "budget": {"soft_max_usd": value}}})
