"""The publish gate: a hit fails the whole run (§5.5.13).

The distinction under test is between "this cluster was skipped" and "this run
published nothing". The first is what the pipeline does with a bad parse; doing
the same with a leak would let the run exit successfully, publish the remaining
clusters, and record the leak as a single counter.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

import pytest

from memory_net.distiller.llm.base import LLMProvider, LLMRequest, LLMResponse
from memory_net.distiller.orchestrator import (
    DistillGateError,
    DistillOrchestrator,
    _distill_gate_error,
)
from memory_net.distiller.output_scanner import OutputScanner, Ruleset
from memory_net.distiller.pipeline import (
    DistillationAborted,
    DistillationOutput,
    DistillationPipeline,
)
from memory_net.distiller.runctx import RunContextBuilder, RunIdentity

PATTERNS_PATH = Path("config/redaction_patterns.yaml")
HOME_PATH = "/" + "home/" + "example-user/notes.md"
RUN_ID = "20260810T101010Z-a1b2c3d4"

CLEAN = json.dumps(
    {
        "products": [
            {
                "title": "Retry token refresh once",
                "category": "best_practice",
                "summary": "Retry once with jitter before failing.",
                "evidence": "Both leaves saw the same failure.",
                "citations": [{"memory_id": "leaf-a-mem-1", "revision_hash": "aaa111"}],
            }
        ]
    }
)
LEAKING = json.dumps(
    {
        "products": [
            {
                "title": "Deploy from the build box",
                "category": "best_practice",
                "summary": f"The playbook lives at {HOME_PATH}.",
                "evidence": "Observed twice.",
                "citations": [{"memory_id": "leaf-b-mem-1", "revision_hash": "bbb222"}],
            }
        ]
    }
)


class _ScriptedProvider(LLMProvider):
    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)
        self.calls = 0

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return LLMResponse(text=self.texts.pop(0), model=request.model)


@pytest.fixture()
def scanner() -> OutputScanner:
    return OutputScanner(Ruleset.from_yaml(PATTERNS_PATH))


#: One record per scripted response, each carrying the revision its product
#: cites, so a citation failure never masquerades as a scanner refusal.
RECORDS = [
    {
        "memory_id": "leaf-a-mem-1",
        "body": "token refresh",
        "revision_hash": "aaa111",
        "author_node": "alice-claude-01",
    },
    {
        "memory_id": "leaf-b-mem-1",
        "body": "deploy window",
        "revision_hash": "bbb222",
        "author_node": "bob-hermes-01",
    },
]


def _records(count: int) -> list[dict[str, Any]]:
    return [dict(record) for record in RECORDS[:count]]


def _pipeline(
    tmp_path: Path, provider: LLMProvider, scanner: OutputScanner | None
) -> DistillationPipeline:
    (tmp_path / "system.md").write_text("system", encoding="utf-8")
    (tmp_path / "user.md").write_text("records: {{records}}", encoding="utf-8")
    context = (
        RunContextBuilder()
        .set_identity(RunIdentity(run_id=RUN_ID, now=lambda: "2026-08-10T00:00:00Z"))
        .set_scanner(scanner)
        .freeze()
    )
    return DistillationPipeline(
        context=context,
        provider=provider,
        system_prompt_path=tmp_path / "system.md",
        user_prompt_path=tmp_path / "user.md",
    )


# --------------------------------------------------------------------------
# The pipeline stops, and says why
# --------------------------------------------------------------------------


def test_a_leaking_product_aborts_the_run_and_carries_the_counts(
    tmp_path: Path, scanner: OutputScanner
) -> None:
    pipeline = _pipeline(tmp_path, _ScriptedProvider([LEAKING]), scanner)

    with pytest.raises(DistillationAborted) as excinfo:
        pipeline.run(_records(1), run_id=RUN_ID)

    output = excinfo.value.output
    assert output.redaction_failures == 1
    assert output.redaction_rule_counts == {"abs_home_path": 1}
    assert output.products == []
    assert HOME_PATH not in str(excinfo.value)


def test_a_clean_run_is_unaffected(tmp_path: Path, scanner: OutputScanner) -> None:
    pipeline = _pipeline(tmp_path, _ScriptedProvider([CLEAN]), scanner)

    output = pipeline.run(_records(1), run_id=RUN_ID)

    assert output.redaction_failures == 0
    assert output.redaction_rule_counts is None
    assert len(output.products) == 1


def test_without_a_scanner_the_pipeline_behaves_exactly_as_before(
    tmp_path: Path,
) -> None:
    """Regression: the leaking response is rendered, as it was before the gate."""
    pipeline = _pipeline(tmp_path, _ScriptedProvider([LEAKING]), None)

    output = pipeline.run(_records(2), run_id=RUN_ID)

    assert output.redaction_failures == 0
    assert len(output.products) == 1


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def test_the_gate_reports_the_refusal_with_rule_names_only() -> None:
    error = _distill_gate_error(
        DistillationOutput(
            products=[],
            response=LLMResponse(text="", model="m"),
            redaction_failures=2,
            redaction_rule_counts={"abs_home_path": 2},
        )
    )

    assert error is not None
    assert "output scanner refusals: 2" in error
    assert "abs_home_path" in error
    assert HOME_PATH not in error


def test_a_second_leaking_cluster_publishes_nothing_at_all(
    tmp_path: Path, scanner: OutputScanner
) -> None:
    """Not "skip the offending cluster": the first cluster's product must not
    reach the team tree either."""
    repo = tmp_path / "repo"
    repo.mkdir()
    provider = _ScriptedProvider([CLEAN, LEAKING])
    pipeline = _pipeline(tmp_path, provider, scanner)
    # One cluster per record. The stand-in embedding backend always yields a
    # single cluster, and "the second cluster leaks" is precisely the case.
    pipeline._cluster_records = (  # type: ignore[method-assign]
        lambda records: [[record] for record in records]
    )
    orchestrator = DistillOrchestrator(
        repo_path=repo,
        pipeline=pipeline,
        context=pipeline.context,
    )
    _stub_repo_io(orchestrator, _records(2))

    with pytest.raises(DistillGateError, match="output scanner refusals"):
        orchestrator._run_at_snapshot(0.0, "deadbeef")

    assert not (repo / "team").exists()
    assert not (repo / "manifest.json").exists()
    assert provider.calls == 2


def test_the_failure_metrics_carry_the_counts_but_no_content(
    tmp_path: Path, scanner: OutputScanner
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    pipeline = _pipeline(tmp_path, _ScriptedProvider([LEAKING]), scanner)
    orchestrator = DistillOrchestrator(
        repo_path=repo, pipeline=pipeline, context=pipeline.context
    )
    _stub_repo_io(orchestrator, _records(1), keep_failure_outputs=True)

    with pytest.raises(DistillGateError):
        orchestrator._run_at_snapshot(0.0, "deadbeef")

    metrics = json.loads(
        (repo / "distill_runs" / RUN_ID / "metrics.json").read_text(encoding="utf-8")
    )
    health = json.loads((repo / ".health.json").read_text(encoding="utf-8"))
    assert metrics["status"] == "failed"
    assert metrics["redaction_failures"] == 1
    assert metrics["redaction_rule_counts"] == {"abs_home_path": 1}
    assert HOME_PATH not in json.dumps(metrics)
    assert HOME_PATH not in json.dumps(health)
    assert health["status"] == "degraded"


def _stub_repo_io(
    orchestrator: DistillOrchestrator,
    records: list[dict[str, Any]],
    *,
    keep_failure_outputs: bool = False,
) -> None:
    """Silence the git-backed inputs; the gate is what is under test."""
    orchestrator._load_leaf_records = lambda: records  # type: ignore[method-assign]
    orchestrator._load_master_manifest = dict  # type: ignore[method-assign]
    orchestrator._snapshot_prompts = (  # type: ignore[method-assign]
        lambda snapshot_sha: contextlib.nullcontext()
    )
    orchestrator._config_revision = (  # type: ignore[method-assign]
        lambda snapshot_sha: "cfg"
    )
    orchestrator._review_gate_status = lambda run_id: "disabled"  # type: ignore[method-assign]
    orchestrator._publish_branch = lambda run_id: "master"  # type: ignore[method-assign]
    if not keep_failure_outputs:
        orchestrator._write_failure_outputs = (  # type: ignore[method-assign]
            lambda **kwargs: None
        )


def test_a_leaking_parse_failure_detail_aborts_the_run(
    tmp_path: Path, scanner: OutputScanner
) -> None:
    """``parse_error_details`` is published in metrics.json and is the one piece
    of model output that reaches an artifact without being parsed first: the
    parser quotes the candidate key names it found."""
    unparseable = 'prelude {"summary-' + HOME_PATH + '": 1} tail'
    pipeline = _pipeline(tmp_path, _ScriptedProvider([unparseable]), scanner)
    pipeline.fallback_provider = _ScriptedProvider([unparseable])

    with pytest.raises(DistillationAborted) as excinfo:
        pipeline.run(_records(1), run_id=RUN_ID)

    output = excinfo.value.output
    assert output.redaction_failures
    assert output.redaction_rule_counts is not None
    assert "abs_home_path" in output.redaction_rule_counts
    assert output.parse_error_details == []
    assert HOME_PATH not in str(excinfo.value)
