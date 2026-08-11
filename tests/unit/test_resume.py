"""``--resume``: what it reuses, and the three things it refuses to reuse.

A resume reuses an earlier run's *inputs* and its finished products. It never
reuses that run's staging, its bundle or its run id, and it never runs across a
master snapshot that has moved -- splicing products derived from two different
snapshots into one release is the same defect ``config_drift_superseded``
exists to refuse.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from memory_net.distiller.cli import _build_parser
from memory_net.distiller.llm.base import LLMProvider, LLMRequest, LLMResponse
from memory_net.distiller.metrics import DistillMetrics
from memory_net.distiller.orchestrator import (
    DistillOrchestrator,
    _accounting_of,
    _with_carried_products,
)
from memory_net.distiller.output_scanner import (
    OutputScanner,
    RedactionViolation,
    Ruleset,
)
from memory_net.distiller.pipeline import DistillationPipeline
from memory_net.distiller.runctx import RunContextBuilder, RunIdentity
from memory_net.shared.models import TeamProduct

PATTERNS_PATH = Path("config/redaction_patterns.yaml")
HOME_PATH = "/" + "home/" + "example-user/runbook.md"
SNAPSHOT_SHA = "a" * 40
PRIOR_RUN = "20260810T090000Z-11112222"

PRODUCT = """---
schema_version: memory-net/team-product/v1
distillation_id: distill-{run}-001
title: Retry policy
category: common_knowledge
topic_id: topic-v1-retry-policy
citations:
- memory_id: leaf-a-mem-1
  revision_hash: rev00001
---

# Retry policy

Use backoff.
"""


class CountingProvider(LLMProvider):
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
            usage={"input_tokens": 1, "output_tokens": 1},
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
    pipeline = DistillationPipeline(
        provider=provider,
        system_prompt_path=system,
        user_prompt_path=user,
    )
    pipeline._cluster_records = lambda records: [  # type: ignore[method-assign]
        [record] for record in records
    ]
    return pipeline


def _prior_run(
    repo: Path,
    *,
    snapshot_sha: str = SNAPSHOT_SHA,
    product: str | None = None,
) -> None:
    root = repo / "distill_runs" / PRIOR_RUN
    (root / "team" / "common_knowledge").mkdir(parents=True)
    (root / "snapshot.json").write_text(
        json.dumps(
            {
                "leaf_refs": {"origin/leaf/alice": "b" * 40},
                "master_snapshot_sha": snapshot_sha,
                "run_id": PRIOR_RUN,
            }
        ),
        encoding="utf-8",
    )
    (root / "team" / "common_knowledge" / "retry-policy.md").write_text(
        product if product is not None else PRODUCT.format(run=PRIOR_RUN),
        encoding="utf-8",
    )


def _orchestrator(
    tmp_path: Path,
    *,
    resume_from: str | None = PRIOR_RUN,
    leaf_refs: dict[str, str] | None = None,
    scanner: OutputScanner | None = None,
    record_bundle_dir: Path | None = None,
) -> DistillOrchestrator:
    context = (
        RunContextBuilder()
        .set_identity(RunIdentity(run_id="run-new", now=lambda: "2026-08-11T00:00:00Z"))
        .set_scanner(scanner)
        .freeze()
    )
    orchestrator = DistillOrchestrator(
        repo_path=tmp_path / "repo",
        pipeline=_pipeline(tmp_path, CountingProvider()),
        resume_from=resume_from,
        record_bundle_dir=record_bundle_dir,
        context=context,
    )
    # What this run just read off the remote, which the prior snapshot has to
    # agree with.
    orchestrator.leaf_refs = (
        {"origin/leaf/alice": "b" * 40} if leaf_refs is None else leaf_refs
    )
    return orchestrator


def test_no_resume_reads_nothing_and_carries_nothing(tmp_path: Path) -> None:
    (tmp_path / "repo").mkdir()

    assert (
        _orchestrator(tmp_path, resume_from=None)._resumed_products(SNAPSHOT_SHA) == []
    )


def test_a_drifted_snapshot_refuses_the_resume(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _prior_run(repo, snapshot_sha="c" * 40)

    with pytest.raises(RuntimeError, match="master snapshot drifted"):
        _orchestrator(tmp_path)._resumed_products(SNAPSHOT_SHA)


def test_a_missing_prior_snapshot_refuses_the_resume(tmp_path: Path) -> None:
    (tmp_path / "repo").mkdir()

    with pytest.raises(RuntimeError, match="no snapshot.json"):
        _orchestrator(tmp_path)._resumed_products(SNAPSHOT_SHA)


def test_leftover_staging_refuses_the_resume(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _prior_run(repo)
    bundle = tmp_path / "record_runs" / "bundle"
    (bundle.parent / f"{bundle.name}.staging-{PRIOR_RUN}").mkdir(parents=True)

    with pytest.raises(RuntimeError, match="leftover record bundle staging"):
        _orchestrator(tmp_path, record_bundle_dir=bundle)._resumed_products(
            SNAPSHOT_SHA
        )


def test_a_carried_product_goes_through_this_runs_scanner(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _prior_run(
        repo,
        product=PRODUCT.format(run=PRIOR_RUN).replace(
            "Use backoff.", f"See {HOME_PATH}."
        ),
    )
    scanner = OutputScanner(Ruleset.from_yaml(PATTERNS_PATH))

    with pytest.raises(RedactionViolation):
        _orchestrator(tmp_path, scanner=scanner)._resumed_products(SNAPSHOT_SHA)


def test_a_clean_carried_product_is_loaded_with_its_frontmatter(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _prior_run(repo)

    carried = _orchestrator(
        tmp_path, scanner=OutputScanner(Ruleset.from_yaml(PATTERNS_PATH))
    )._resumed_products(SNAPSHOT_SHA)

    assert len(carried) == 1
    assert carried[0].relative_path.as_posix() == (
        "team/common_knowledge/retry-policy.md"
    )
    assert carried[0].category == "common_knowledge"
    # The earlier run's id stays on the product it produced: rewriting it would
    # claim this run generated evidence it never saw.
    assert carried[0].distillation_id == f"distill-{PRIOR_RUN}-001"


def test_a_finished_cluster_is_skipped_and_terminates_as_resumed(
    tmp_path: Path,
) -> None:
    provider = CountingProvider()
    pipeline = _pipeline(tmp_path, provider)
    records = [_record(1), _record(2)]

    output = pipeline.run(
        records,
        run_id="run-new",
        resume_completed_citations={"leaf-a-mem-1@rev00001"},
    )

    assert provider.calls == [["leaf-a-mem-2"]]
    assert output.cluster_terminal_counts is not None
    assert output.cluster_terminal_counts["resumed"] == 1
    assert output.cluster_terminal_counts["distilled"] == 1
    # The carried products cite the skipped record, so nothing is unclustered.
    assert output.unclustered == []
    assert _accounting_of(output, len(records)).mismatch is False


def test_carrying_products_keeps_the_category_distribution_whole(
    tmp_path: Path,
) -> None:
    provider = CountingProvider()
    pipeline = _pipeline(tmp_path, provider)
    carried = [
        TeamProduct(
            relative_path=Path("team/common_knowledge/retry-policy.md"),
            content=PRODUCT.format(run=PRIOR_RUN),
            frontmatter={"citations": [{"memory_id": "leaf-a-mem-1"}]},
            category="common_knowledge",
            distillation_id=f"distill-{PRIOR_RUN}-001",
        )
    ]
    output = pipeline.run(
        [_record(1), _record(2)],
        run_id="run-new",
        resume_completed_citations={"leaf-a-mem-1@rev00001"},
    )

    merged = _with_carried_products(output, carried)

    assert len(merged.products) == 2
    assert merged.category_distribution is not None
    assert sum(merged.category_distribution.values()) == len(merged.products)


def test_metrics_record_which_run_was_continued() -> None:
    metrics = DistillMetrics(
        run_id="run-new",
        input_records=2,
        products=2,
        resumed_from=PRIOR_RUN,
    ).as_dict()

    assert metrics["resumed_from"] == PRIOR_RUN
    assert (
        DistillMetrics(run_id="r", input_records=0, products=0).as_dict()[
            "resumed_from"
        ]
        == ""
    )


def test_the_flag_defaults_to_no_resume() -> None:
    args = _build_parser().parse_args(["run", "--repo", "."])

    assert args.resume is None
    assert (
        _build_parser().parse_args(["run", "--repo", ".", "--resume", "r1"]).resume
        == "r1"
    )


def test_a_moved_leaf_refuses_the_resume(tmp_path: Path) -> None:
    """Master can sit still while the input the products cite is edited."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _prior_run(repo)

    with pytest.raises(RuntimeError, match="leaf refs drifted"):
        _orchestrator(
            tmp_path, leaf_refs={"origin/leaf/alice": "c" * 40}
        )._resumed_products(SNAPSHOT_SHA)


def test_a_resume_id_that_is_a_path_is_refused(tmp_path: Path) -> None:
    (tmp_path / "repo").mkdir()

    for value in ("../../elsewhere", "/etc", "a/b", ".."):
        with pytest.raises(RuntimeError, match="not a run id"):
            _orchestrator(tmp_path, resume_from=value)._resumed_products(SNAPSHOT_SHA)


def test_a_symlinked_run_directory_is_refused(tmp_path: Path) -> None:
    """A valid run id whose directory points out of the repository."""
    repo = tmp_path / "repo"
    (repo / "distill_runs").mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "team" / "common_knowledge").mkdir(parents=True)
    (outside / "snapshot.json").write_text(
        json.dumps(
            {
                "leaf_refs": {"origin/leaf/alice": "b" * 40},
                "master_snapshot_sha": SNAPSHOT_SHA,
                "run_id": PRIOR_RUN,
            }
        ),
        encoding="utf-8",
    )
    (outside / "team" / "common_knowledge" / "retry-policy.md").write_text(
        PRODUCT.format(run=PRIOR_RUN), encoding="utf-8"
    )
    (repo / "distill_runs" / PRIOR_RUN).symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="symbolic link"):
        _orchestrator(tmp_path)._resumed_products(SNAPSHOT_SHA)


def test_a_symlinked_snapshot_is_refused(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _prior_run(repo)
    root = repo / "distill_runs" / PRIOR_RUN
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text((root / "snapshot.json").read_text(encoding="utf-8"), "utf-8")
    (root / "snapshot.json").unlink()
    (root / "snapshot.json").symlink_to(elsewhere)

    with pytest.raises(RuntimeError, match="symbolic link"):
        _orchestrator(tmp_path)._resumed_products(SNAPSHOT_SHA)


def test_a_symlinked_product_is_refused(tmp_path: Path) -> None:
    """The one that still passes the scanner: the text is fine, the origin is not."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _prior_run(repo)
    product = (
        repo
        / "distill_runs"
        / PRIOR_RUN
        / "team"
        / "common_knowledge"
        / ("retry-policy.md")
    )
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text(product.read_text(encoding="utf-8"), encoding="utf-8")
    product.unlink()
    product.symlink_to(elsewhere)

    with pytest.raises(RuntimeError, match="symbolic link"):
        _orchestrator(tmp_path)._resumed_products(SNAPSHOT_SHA)


def test_an_edited_record_is_not_treated_as_already_distilled(
    tmp_path: Path,
) -> None:
    """The earlier product cites rev00001; the record now reads rev99999."""
    provider = CountingProvider()
    pipeline = _pipeline(tmp_path, provider)
    edited = _record(1)
    edited["revision_hash"] = "rev99999"

    output = pipeline.run(
        [edited],
        run_id="run-new",
        resume_completed_citations={"leaf-a-mem-1@rev00001"},
    )

    assert provider.calls == [["leaf-a-mem-1"]]
    assert output.cluster_terminal_counts is not None
    assert output.cluster_terminal_counts["resumed"] == 0
    assert output.cluster_terminal_counts["distilled"] == 1
