"""``review_request.json``: what it may say, and when it exists at all.

The file travels with a release branch to a human reviewer. Two things are load
bearing: it names no product text and no reviewer account, and it is written
before the commit -- written after the push it would be in neither the commit
nor the pull request, which is the only place a reviewer would look for it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from memory_net.distiller.llm.base import LLMResponse
from memory_net.distiller.metrics import DistillMetrics
from memory_net.distiller.orchestrator import DistillOrchestrator
from memory_net.distiller.pipeline import DistillationOutput, DistillationPipeline
from memory_net.distiller.publisher import (
    FIELDS,
    REVIEW_GATE_STATUSES,
    SCHEMA_VERSION,
    ReviewRequestError,
    build_review_request,
    validate_review_request,
)
from memory_net.shared.models import TeamProduct

RUN_ID = "20260811T101010Z-abcd1234"
SNAPSHOT_SHA = "a" * 40
PRODUCT_TEXT = "the retry budget was raised after the october incident"


def _request(**overrides: Any) -> dict[str, Any]:
    payload = build_review_request(
        run_id=RUN_ID,
        head=f"release/{RUN_ID}",
        base="master",
        codeowners_path=".github/CODEOWNERS",
        master_snapshot_sha=SNAPSHOT_SHA,
        products_count=2,
        categories=["common_knowledge"],
    )
    payload.update(overrides)
    return payload


def test_a_request_carries_the_pointers_and_nothing_else() -> None:
    payload = _request()

    assert sorted(payload) == sorted(FIELDS)
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["head"] == f"release/{RUN_ID}"
    assert payload["base"] == "master"
    assert payload["title"] == f"distill run {RUN_ID}"
    assert payload["metrics_path"] == f"distill_runs/{RUN_ID}/metrics.json"
    assert payload["codeowners_path"] == ".github/CODEOWNERS"


def test_an_extra_key_is_refused() -> None:
    with pytest.raises(ReviewRequestError, match="unknown keys"):
        validate_review_request(_request(summary="two products about retries"))


def test_a_missing_key_is_refused() -> None:
    payload = _request()
    del payload["master_snapshot_sha"]

    with pytest.raises(ReviewRequestError, match="missing keys"):
        validate_review_request(payload)


@pytest.mark.parametrize(
    "head",
    ["release/run id", "release/$(whoami)", "--head=x", ""],
)
def test_a_head_that_is_not_a_branch_name_is_refused(head: str) -> None:
    with pytest.raises(ReviewRequestError):
        validate_review_request(_request(head=head))


@pytest.mark.parametrize("path", ["/etc/CODEOWNERS", "../../CODEOWNERS"])
def test_a_codeowners_path_outside_the_repo_is_refused(path: str) -> None:
    with pytest.raises(ReviewRequestError, match="stay inside the repo"):
        validate_review_request(_request(codeowners_path=path))


def test_a_free_text_title_is_refused() -> None:
    with pytest.raises(ReviewRequestError, match="derived from the run id"):
        validate_review_request(_request(title="please merge, the model said so"))


def test_a_category_outside_the_configured_set_is_refused() -> None:
    with pytest.raises(ReviewRequestError, match="configured set"):
        build_review_request(
            run_id=RUN_ID,
            head=f"release/{RUN_ID}",
            base="master",
            codeowners_path=".github/CODEOWNERS",
            master_snapshot_sha=SNAPSHOT_SHA,
            products_count=1,
            categories=["invented_category"],
            allowed_categories={"common_knowledge"},
        )


def test_a_negative_product_count_is_refused() -> None:
    with pytest.raises(ReviewRequestError, match="must be a count"):
        validate_review_request(_request(products_count=-1))


def test_the_review_gate_vocabulary_covers_all_four_states() -> None:
    assert REVIEW_GATE_STATUSES == ("disabled", "pending", "approved", "rejected")
    for status in REVIEW_GATE_STATUSES:
        metrics = DistillMetrics(
            run_id=RUN_ID,
            input_records=0,
            products=0,
            review_gate_status=status,
        )
        assert json.loads(json.dumps(metrics.as_dict()))["review_gate_status"] == status


# --------------------------------------------------------------------------
# The orchestrator's write point
# --------------------------------------------------------------------------


def _orchestrator(tmp_path: Path, *, template: str | None) -> DistillOrchestrator:
    system = tmp_path / "system.md"
    user = tmp_path / "user.md"
    system.write_text("system", encoding="utf-8")
    user.write_text("records", encoding="utf-8")
    pipeline = DistillationPipeline(
        provider=_NullProvider(),
        system_prompt_path=system,
        user_prompt_path=user,
        categories=["common_knowledge", "best_practices"],
    )
    return DistillOrchestrator(
        repo_path=tmp_path / "repo",
        pipeline=pipeline,
        publish_branch_template=template,
    )


def _output() -> DistillationOutput:
    product = TeamProduct(
        relative_path=Path("team/common_knowledge/retry-policy.md"),
        content=f"# Retry policy\n\n{PRODUCT_TEXT}\n",
        frontmatter={"citations": [{"memory_id": "leaf-a-mem-1"}]},
        category="common_knowledge",
        distillation_id=f"distill-{RUN_ID}-001",
    )
    return DistillationOutput(
        products=[product],
        response=LLMResponse(text="", model="m"),
    )


def test_the_request_is_written_when_the_gate_is_on(tmp_path: Path) -> None:
    (tmp_path / "repo").mkdir()
    orchestrator = _orchestrator(tmp_path, template="release/<run_id>")

    orchestrator._write_review_request(
        run_id=RUN_ID, snapshot_sha=SNAPSHOT_SHA, output=_output()
    )

    text = (
        tmp_path / "repo" / "distill_runs" / RUN_ID / "review_request.json"
    ).read_text(encoding="utf-8")
    payload = json.loads(text)
    validate_review_request(payload)
    assert payload["products_count"] == 1
    assert payload["categories"] == ["common_knowledge"]
    assert payload["master_snapshot_sha"] == SNAPSHOT_SHA
    # Summary and pointers only: no product body, no reviewer account.
    assert not re.search(re.escape(PRODUCT_TEXT), text)
    assert not re.search(r"@[A-Za-z0-9-]+", text)


def test_nothing_is_written_when_the_gate_is_off(tmp_path: Path) -> None:
    (tmp_path / "repo").mkdir()
    orchestrator = _orchestrator(tmp_path, template=None)

    orchestrator._write_review_request(
        run_id=RUN_ID, snapshot_sha=SNAPSHOT_SHA, output=_output()
    )

    assert not (tmp_path / "repo" / "distill_runs").exists()


def test_the_request_precedes_the_staging_of_the_release_commit(
    tmp_path: Path,
) -> None:
    """The order is the point: staged, then committed, then pushed."""
    (tmp_path / "repo").mkdir()
    orchestrator = _orchestrator(tmp_path, template="release/<run_id>")
    seen: list[str] = []

    def spy_add(*args: str, **kwargs: Any) -> Any:
        seen.append(args[0])
        raise _StopHere

    orchestrator.repo.run = spy_add  # type: ignore[method-assign]
    orchestrator._ensure_master_unchanged = lambda *a, **k: None  # type: ignore[method-assign]
    orchestrator._enforce_master_path_whitelist = lambda *a, **k: None  # type: ignore[method-assign]

    with pytest.raises(_StopHere):
        orchestrator._publish_outputs(
            snapshot_sha=SNAPSHOT_SHA, run_id=RUN_ID, output=_output()
        )

    assert seen == ["add"]
    assert (
        tmp_path / "repo" / "distill_runs" / RUN_ID / "review_request.json"
    ).exists()


class _StopHere(Exception):
    pass


class _NullProvider:
    def generate(self, request: Any) -> LLMResponse:  # pragma: no cover - unused
        raise AssertionError("no call is made in these tests")
