"""M4-004: the mock lane and the production lane satisfy one contract (§9.12).

The two lanes are a mock run and a *replayed* recording of a real DeepSeek run,
over the same records under the same clustering. `hermes_tool` is not a lane
here: it is an experimental entry point whose backend is itself the mock, so
comparing against it compares the mock with itself.

Byte equality is neither available nor wanted -- a real model writes its own
prose, so titles differ and every id derived from them differs too. What is
held equal is the contract, and eight of its seventeen clauses exist because
the other nine are all satisfied by a run that produces nothing at all.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from memory_net.distiller.cli import main
from memory_net.distiller.golden_eval import (
    Expectations,
    LaneOutcome,
    assert_invariants_equivalent,
    compare_run_invariants,
    render_invariant_table,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_REPO_ASSETS = REPO_ROOT / "tests" / "fixtures" / "golden_repo"
BUNDLE_ROOT = REPO_ROOT / "tests" / "fixtures" / "golden_bundle"

PRODUCTION_PROFILE = "config/distill.deepseek.yaml"
MOCK_PROFILE = "config/distill.mock.yaml"

EXPECT = Expectations(
    # One product per cluster, all three in the one category the fixture's
    # records hint at. Declared rather than derived: a comparison against
    # whatever the run produced cannot fail.
    products_per_category={"common_knowledge": 3},
    max_products_per_run=6,
    max_products_per_cluster=2,
    min_summary_chars=40,
    min_citation_coverage=0.5,
    min_citations_by_category={"common_knowledge": 1},
    min_contributors_by_category={"common_knowledge": 1},
    required_terms=("deploy window", "search index"),
)


def _load_builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "equivalence_build_repo", GOLDEN_REPO_ASSETS / "build_repo.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


builder = _load_builder()


def _bundle() -> Path:
    return BUNDLE_ROOT / (BUNDLE_ROOT / "CURRENT").read_text(encoding="utf-8").strip()


def _categories() -> list[str]:
    profile = yaml.safe_load(
        (GOLDEN_REPO_ASSETS / PRODUCTION_PROFILE).read_text(encoding="utf-8")
    )
    return [str(item) for item in profile["categories"]]


def _run(tmp_path: Path, name: str, argv: list[str]) -> LaneOutcome:
    repo = builder.build(tmp_path / name).work
    assert main(["run", "--repo", str(repo), "--no-push", *argv]) == 0
    run_id = str(json.loads((repo / "manifest.json").read_text())["last_run_id"])
    return LaneOutcome.collect(repo, name=name, run_id=run_id, categories=_categories())


@pytest.fixture(scope="module")
def lanes(tmp_path_factory: pytest.TempPathFactory) -> tuple[LaneOutcome, LaneOutcome]:
    root = tmp_path_factory.mktemp("equivalence")
    mock = _run(root, "mock", ["--config", MOCK_PROFILE, "--provider", "mock"])
    real = _run(
        root,
        "deepseek-replay",
        [
            "--config",
            PRODUCTION_PROFILE,
            "--provider",
            "deepseek",
            "--replay",
            str(_bundle()),
            "--replay-mode",
            "strict",
        ],
    )
    return mock, real


@pytest.fixture(scope="module")
def clusters() -> list[dict[str, Any]]:
    return list(
        json.loads((_bundle() / "cluster_assignment.json").read_text())["clusters"]
    )


def test_the_two_lanes_really_are_two_different_backends(
    lanes: tuple[LaneOutcome, LaneOutcome],
) -> None:
    """Guard against the comparison degenerating into mock-versus-mock, which
    is what the pre-existing bridge "equivalence" test actually was."""
    mock, real = lanes

    assert mock.metrics["provider_backends"] == ["mock"]
    assert real.metrics["provider_backends"] == ["deepseek"]
    assert mock.metrics["usage_source"] == "estimated"
    assert real.metrics["usage_source"] == "reported"
    # And the outputs are genuinely different prose, so the invariants below
    # are doing work rather than comparing identical files.
    assert set(mock.products) != set(real.products)


def test_the_lanes_satisfy_the_same_contract(
    lanes: tuple[LaneOutcome, LaneOutcome], clusters: list[dict[str, Any]]
) -> None:
    results = compare_run_invariants(*lanes, clusters=clusters, expect=EXPECT)

    assert len(results) == 18  # 17 numbered clauses; 8 is split into two
    assert_invariants_equivalent(results)


def test_the_contract_table_names_every_invariant(
    lanes: tuple[LaneOutcome, LaneOutcome], clusters: list[dict[str, Any]]
) -> None:
    """The artifact a reviewer reads: seventeen named lines, not one boolean."""
    table = render_invariant_table(
        compare_run_invariants(*lanes, clusters=clusters, expect=EXPECT)
    ).splitlines()

    assert len(table) == 18
    assert all(line.startswith("PASS") for line in table)


# -- the negative cases --------------------------------------------------
#
# A comparator that cannot fail is a comparator that says nothing, so each of
# the three defects the invariants were written for is injected here and the
# specific clause that must catch it is named.


def _failed(results: list[Any]) -> set[int]:
    return {item.number for item in results if not item.passed}


def test_a_broken_citation_is_reported(
    lanes: tuple[LaneOutcome, LaneOutcome], clusters: list[dict[str, Any]]
) -> None:
    mock, real = lanes
    path = next(iter(real.products))
    damaged = replace(
        real,
        products={
            **real.products,
            path: {**real.products[path], "citations": []},
        },
    )

    failed = _failed(
        compare_run_invariants(mock, damaged, clusters=clusters, expect=EXPECT)
    )
    # 14 is the per-product clause; 15 is the cluster's coverage of its input.
    assert 14 in failed
    assert 15 in failed


def test_an_empty_product_set_is_reported(
    lanes: tuple[LaneOutcome, LaneOutcome], clusters: list[dict[str, Any]]
) -> None:
    """The defect the eight added clauses exist for: this run passes schema
    validation, and its citation resolution rate is 1.0 by definition."""
    mock, real = lanes
    empty = replace(real, products={}, bodies={})

    failed = _failed(
        compare_run_invariants(mock, empty, clusters=clusters, expect=EXPECT)
    )
    assert {10, 11, 15, 17} <= failed


def test_a_duplicate_topic_is_reported(
    lanes: tuple[LaneOutcome, LaneOutcome], clusters: list[dict[str, Any]]
) -> None:
    mock, real = lanes
    first, second = list(real.products)[:2]
    duplicated = replace(
        real,
        products={
            **real.products,
            second: {
                **real.products[second],
                "topic_id": real.products[first]["topic_id"],
            },
        },
    )

    failed = _failed(
        compare_run_invariants(mock, duplicated, clusters=clusters, expect=EXPECT)
    )
    assert 16 in failed


def test_both_lanes_failing_to_parse_is_reported(
    lanes: tuple[LaneOutcome, LaneOutcome], clusters: list[dict[str, Any]]
) -> None:
    """Clause 8 used to fold this into a cross-lane equality, which both lanes
    satisfied by failing identically: `(keys, False) == (keys, False)`.

    A property that has to hold is the wrong thing to express as a comparison
    -- an equality between two broken lanes is still an equality.
    """
    mock, real = lanes
    broken = [
        replace(lane, metrics={**lane.metrics, "parse_failure_rate": 0.25})
        for lane in (mock, real)
    ]

    failed = _failed(compare_run_invariants(*broken, clusters=clusters, expect=EXPECT))
    assert 8 in failed


def test_one_rejection_does_not_account_for_several_silent_clusters(
    lanes: tuple[LaneOutcome, LaneOutcome], clusters: list[dict[str, Any]]
) -> None:
    """Clause 10 used to ask whether *any* rejection was recorded, so a lane
    that dropped every cluster but rejected one read as fully accounted for."""
    mock, real = lanes
    silent = replace(
        real,
        products={},
        bodies={},
        metrics={**real.metrics, "skipped_clusters": 1},
    )

    failed = _failed(
        compare_run_invariants(mock, silent, clusters=clusters, expect=EXPECT)
    )
    assert 10 in failed


def test_a_fully_accounted_for_rejection_does_not_trip_clause_ten(
    lanes: tuple[LaneOutcome, LaneOutcome], clusters: list[dict[str, Any]]
) -> None:
    """The other side of the same clause: a cluster that produced nothing *and*
    has a recorded reason is not a silent drop, and must not be reported as one.
    """
    mock, real = lanes
    path = next(iter(real.products))
    dropped = replace(
        real,
        products={key: value for key, value in real.products.items() if key != path},
        bodies={key: value for key, value in real.bodies.items() if key != path},
        metrics={**real.metrics, "skipped_clusters": 1},
    )

    failed = _failed(
        compare_run_invariants(mock, dropped, clusters=clusters, expect=EXPECT)
    )
    assert 10 not in failed
