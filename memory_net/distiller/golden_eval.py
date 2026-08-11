from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from memory_net.distiller.llm.mock import MockLLMProvider
from memory_net.distiller.pipeline import DistillationPipeline
from memory_net.shared.schema import validate_team_product_frontmatter
from memory_net.shared.util.frontmatter import split_frontmatter


def evaluate_golden_fixture(fixture: Path) -> dict[str, Any]:
    if not (fixture / "records.json").exists():
        return _evaluate_suite(fixture)
    return _evaluate_one_fixture(fixture)


def _evaluate_suite(root: Path) -> dict[str, Any]:
    fixtures = sorted(
        path for path in root.iterdir() if (path / "records.json").exists()
    )
    if not fixtures:
        raise FileNotFoundError(f"golden fixture not found: {root}")
    results = [_evaluate_one_fixture(path) for path in fixtures]
    return {
        "schema_version": "memory-net/golden-eval-suite/v1",
        "passed": all(result["passed"] for result in results),
        "fixture_count": len(results),
        "fixtures": results,
    }


def _evaluate_one_fixture(fixture: Path) -> dict[str, Any]:
    records = json.loads((fixture / "records.json").read_text(encoding="utf-8"))
    expected = json.loads((fixture / "expected.json").read_text(encoding="utf-8"))
    output = DistillationPipeline(
        provider=MockLLMProvider(),
        system_prompt_path=fixture / "system.md",
        user_prompt_path=fixture / "user.md",
    ).run(records, run_id="golden")
    semantic_assertions = _semantic_assertions(fixture, expected, output)
    passed = len(output.products) >= int(expected.get("min_products", 1))
    if expected.get("required_category"):
        passed = passed and any(
            product.category == expected["required_category"]
            for product in output.products
        )
    passed = passed and all(item["passed"] for item in semantic_assertions)
    return {
        "schema_version": "memory-net/golden-eval/v1",
        "fixture": fixture.name,
        "passed": passed,
        "products": len(output.products),
        "categories": [product.category for product in output.products],
        "semantic_assertions": semantic_assertions,
    }


def _semantic_assertions(
    fixture: Path,
    expected: dict[str, Any],
    output: Any,
) -> list[dict[str, Any]]:
    required_terms = _required_terms(fixture, expected)
    if not required_terms:
        return [
            {
                "name": "required_terms_present",
                "passed": False,
                "message": "golden fixture must define required_terms",
            }
        ]
    text = "\n".join(product.content for product in output.products)
    return [{"term": str(term), "passed": str(term) in text} for term in required_terms]


def _required_terms(fixture: Path, expected: dict[str, Any]) -> list[str]:
    assertions_path = fixture / "assertions.yaml"
    if assertions_path.exists():
        value = yaml.safe_load(assertions_path.read_text(encoding="utf-8")) or {}
        if not isinstance(value, dict):
            raise ValueError(f"assertions must be a mapping: {assertions_path}")
        raw = value.get("required_terms") or value.get("semantic_assertions")
    else:
        raw = expected.get("required_terms")
    return [str(item) for item in raw or []]


# -- M4-004: the equivalence contract between two backends ------------------
#
# Byte equality across backends is not available and not desirable: the mock
# renders from a template while a real model writes its own prose, so titles
# differ, and the topic ids derived from them differ with them. What can be
# held equal is the *contract* -- the set of properties a run's output must
# have regardless of which backend produced it.
#
# Eight of the seventeen exist because the first nine can all be satisfied by a
# run that produces nothing: zero citations makes the resolution rate 1.0 by
# definition, and an empty product list passes every schema. So the later
# invariants assert presence and bounds, not just well-formedness.


@dataclass(frozen=True)
class Expectations:
    """What the fixture declares its two lanes must both produce."""

    products_per_category: dict[str, int]
    max_products_per_run: int
    max_products_per_cluster: int
    min_summary_chars: int
    min_citation_coverage: float
    min_citations_by_category: dict[str, int]
    min_contributors_by_category: dict[str, int]
    required_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class LaneOutcome:
    """One backend's published run, read back as data."""

    name: str
    metrics: dict[str, Any]
    manifest: dict[str, Any]
    #: repo-relative path -> parsed frontmatter
    products: dict[str, dict[str, Any]]
    #: repo-relative path -> body text
    bodies: dict[str, str]
    categories: tuple[str, ...]

    @classmethod
    def collect(
        cls,
        repo: Path,
        *,
        name: str,
        run_id: str,
        categories: Sequence[str],
    ) -> LaneOutcome:
        """Read the run's *own* published snapshot, not the repository's ``team/``.

        The latter holds whichever run published last, so two lanes read from
        it would compare the same files against themselves.
        """
        run_dir = repo / "distill_runs" / run_id
        products: dict[str, dict[str, Any]] = {}
        bodies: dict[str, str] = {}
        for path in sorted((run_dir / "team").rglob("*.md")):
            frontmatter, body = split_frontmatter(path.read_text(encoding="utf-8"))
            key = f"team/{path.relative_to(run_dir / 'team').as_posix()}"
            products[key] = frontmatter
            bodies[key] = body
        return cls(
            name=name,
            metrics=json.loads((run_dir / "metrics.json").read_text(encoding="utf-8")),
            manifest=json.loads(
                (run_dir / "manifest.json").read_text(encoding="utf-8")
            ),
            products=products,
            bodies=bodies,
            categories=tuple(categories),
        )


@dataclass(frozen=True)
class InvariantResult:
    number: int
    name: str
    passed: bool
    detail: str


def compare_run_invariants(
    mock: LaneOutcome,
    real: LaneOutcome,
    *,
    clusters: Sequence[Mapping[str, Any]],
    expect: Expectations,
) -> list[InvariantResult]:
    """Check the seventeen invariants over both lanes (§9.12).

    ``clusters`` is the recorded cluster assignment. Both lanes are given the
    same records under the same clustering configuration, and invariant 10
    checks that they agree on the count -- the per-cluster invariants then have
    a partition to be stated against.
    """
    results: list[InvariantResult] = []
    lanes = (mock, real)
    partition = {
        int(cluster["cluster_id"]): [str(item) for item in cluster["memory_ids"]]
        for cluster in clusters
    }

    results.append(
        _both(1, "run status is success", lanes, lambda lane: lane.metrics["status"])
    )
    results.append(
        _equal(2, "input_records", lanes, lambda lane: lane.metrics["input_records"])
    )
    results.append(_all_products(3, "product frontmatter validates", lanes, _schema_ok))
    results.append(
        _both(
            4,
            "citation_resolution_rate is 1.0",
            lanes,
            lambda lane: lane.metrics["citation_resolution_rate"] == 1.0,
        )
    )
    results.append(
        _both(
            5,
            "category_distribution is within the configured categories",
            lanes,
            lambda lane: set(lane.metrics["category_distribution"]).issubset(
                lane.categories
            ),
        )
    )
    results.append(
        _check(6, "category evidence rules hold", _evidence_rules(lanes, expect))
    )
    results.append(
        _equal(
            7,
            "manifest field set and tombstone state",
            lanes,
            lambda lane: (
                tuple(sorted(lane.manifest)),
                bool(lane.manifest.get("products_tombstones")),
            ),
        )
    )
    # Split deliberately. Folding "no parse failures" into the cross-lane
    # comparison made it satisfiable by both lanes failing: `(keys, False)`
    # equals `(keys, False)`. An equality is the wrong shape for a property that
    # has to hold, rather than merely to match.
    results.append(
        _equal(8, "metrics key set", lanes, lambda lane: tuple(sorted(lane.metrics)))
    )
    results.append(
        _both(
            8,
            "no parse failures in either lane",
            lanes,
            lambda lane: lane.metrics["parse_failure_rate"] == 0.0,
        )
    )
    results.append(
        _check(
            9, "fixture required terms appear", _required_terms_present(lanes, expect)
        )
    )
    results.append(
        _check(
            10,
            "every cluster reaches a conclusion",
            _clusters_concluded(lanes, partition),
        )
    )
    results.append(
        _check(11, "product count lower bounds", _lower_bounds(lanes, partition))
    )
    results.append(
        _check(
            12, "product count upper bounds", _upper_bounds(lanes, partition, expect)
        )
    )
    results.append(
        _check(
            13, "title, summary and evidence are non-empty", _non_empty(lanes, expect)
        )
    )
    results.append(
        _check(
            14,
            "per-product citations and contributors",
            _per_product_evidence(lanes, expect),
        )
    )
    results.append(
        _check(
            15, "citation coverage of each cluster", _coverage(lanes, partition, expect)
        )
    )
    results.append(_check(16, "topics are unique", _unique_topics(lanes, partition)))
    results.append(
        _check(17, "fixture-declared shape matches", _declared_shape(lanes, expect))
    )
    return results


def render_invariant_table(results: Sequence[InvariantResult]) -> str:
    return "\n".join(
        f"{'PASS' if item.passed else 'FAIL'} {item.number:2d} {item.name}"
        + (f" -- {item.detail}" if item.detail else "")
        for item in results
    )


def assert_invariants_equivalent(results: Sequence[InvariantResult]) -> None:
    failures = [item for item in results if not item.passed]
    if failures:
        raise AssertionError(
            "provider equivalence contract violated:\n"
            + render_invariant_table(results)
        )


# -- invariant helpers -------------------------------------------------------


def _check(number: int, name: str, problems: list[str]) -> InvariantResult:
    return InvariantResult(number, name, not problems, "; ".join(problems))


def _both(
    number: int,
    name: str,
    lanes: Sequence[LaneOutcome],
    read: Callable[[LaneOutcome], Any],
) -> InvariantResult:
    """Both lanes satisfy the property, stated per lane so the failure names one."""
    problems = [
        f"{lane.name}={read(lane)!r}"
        for lane in lanes
        if read(lane) not in (True, "success")
    ]
    return _check(number, name, problems)


def _equal(
    number: int,
    name: str,
    lanes: Sequence[LaneOutcome],
    read: Callable[[LaneOutcome], Any],
) -> InvariantResult:
    values = {lane.name: read(lane) for lane in lanes}
    distinct = {repr(value) for value in values.values()}
    return _check(
        number,
        name,
        (
            []
            if len(distinct) == 1
            else [f"{key}={value!r}" for key, value in values.items()]
        ),
    )


def _all_products(
    number: int,
    name: str,
    lanes: Sequence[LaneOutcome],
    check: Callable[[dict[str, Any]], str | None],
) -> InvariantResult:
    problems = [
        f"{lane.name}:{path}: {problem}"
        for lane in lanes
        for path, frontmatter in lane.products.items()
        if (problem := check(frontmatter)) is not None
    ]
    return _check(number, name, problems)


def _schema_ok(frontmatter: dict[str, Any]) -> str | None:
    try:
        validate_team_product_frontmatter(frontmatter)
    except Exception as exc:  # noqa: BLE001 - the message is the report
        return str(exc)
    return None


def _cluster_of(memory_id: str, partition: Mapping[int, Sequence[str]]) -> int | None:
    for cluster_id, members in partition.items():
        if memory_id in members:
            return cluster_id
    return None


def _by_cluster(
    lane: LaneOutcome, partition: Mapping[int, Sequence[str]]
) -> dict[int, list[str]]:
    """Products grouped by the cluster their citations came from."""
    grouped: dict[int, list[str]] = {cluster_id: [] for cluster_id in partition}
    for path, frontmatter in lane.products.items():
        for citation in frontmatter.get("citations") or []:
            cluster_id = _cluster_of(str(citation.get("memory_id")), partition)
            if cluster_id is not None:
                grouped[cluster_id].append(path)
                break
    return grouped


def _cited_ids(frontmatter: Mapping[str, Any]) -> list[str]:
    return [str(item.get("memory_id")) for item in frontmatter.get("citations") or []]


def _evidence_rules(lanes: Sequence[LaneOutcome], expect: Expectations) -> list[str]:
    problems: list[str] = []
    for lane in lanes:
        for category, minimum in expect.min_citations_by_category.items():
            total = sum(
                len(_cited_ids(frontmatter))
                for frontmatter in lane.products.values()
                if frontmatter.get("category") == category
            )
            if lane.metrics["category_distribution"].get(category) and total < minimum:
                problems.append(f"{lane.name}:{category} cited {total} < {minimum}")
    return problems


def _required_terms_present(
    lanes: Sequence[LaneOutcome], expect: Expectations
) -> list[str]:
    if not expect.required_terms:
        return []
    problems: list[str] = []
    for lane in lanes:
        text = "\n".join(lane.bodies.values())
        problems.extend(
            f"{lane.name}: missing {term!r}"
            for term in expect.required_terms
            if term not in text
        )
    return problems


def _clusters_concluded(
    lanes: Sequence[LaneOutcome], partition: Mapping[int, Sequence[str]]
) -> list[str]:
    """A cluster that produced nothing must say why, not go quiet.

    An empty product list passes every schema check above it, so "no output"
    has to be distinguishable from "output withheld for a stated reason".
    """
    problems: list[str] = []
    for lane in lanes:
        if lane.metrics["clusters"] != len(partition):
            problems.append(
                f"{lane.name}: clustered into {lane.metrics['clusters']}, "
                f"expected {len(partition)}"
            )
        rejected = (
            int(lane.metrics.get("skipped_clusters", 0))
            + int(lane.metrics.get("evidence_rule_rejections", 0))
            + int(lane.metrics.get("parse_failures", 0))
        )
        silent = [
            cluster_id
            for cluster_id, paths in _by_cluster(lane, partition).items()
            if not paths
        ]
        # Counted, not merely present. A single rejection anywhere in the run
        # used to excuse every silent cluster, so a lane that dropped four
        # clusters and rejected one still read as fully accounted for. The
        # metrics carry no per-cluster reason, so the strongest statement
        # available is that there are at least as many recorded rejections as
        # there are clusters with nothing to show.
        if len(silent) > rejected:
            problems.append(
                f"{lane.name}: clusters {silent} produced nothing, and only "
                f"{rejected} rejection(s) are recorded to account for them"
            )
    return problems


def _lower_bounds(
    lanes: Sequence[LaneOutcome], partition: Mapping[int, Sequence[str]]
) -> list[str]:
    problems: list[str] = []
    for lane in lanes:
        if not lane.products:
            problems.append(f"{lane.name}: produced no products at all")
        for cluster_id, paths in _by_cluster(lane, partition).items():
            if not paths:
                problems.append(f"{lane.name}: cluster {cluster_id} produced none")
    return problems


def _upper_bounds(
    lanes: Sequence[LaneOutcome],
    partition: Mapping[int, Sequence[str]],
    expect: Expectations,
) -> list[str]:
    problems: list[str] = []
    for lane in lanes:
        if len(lane.products) > expect.max_products_per_run:
            problems.append(
                f"{lane.name}: {len(lane.products)} products "
                f"> {expect.max_products_per_run}"
            )
        for cluster_id, paths in _by_cluster(lane, partition).items():
            if len(paths) > expect.max_products_per_cluster:
                problems.append(
                    f"{lane.name}: cluster {cluster_id} produced {len(paths)} "
                    f"> {expect.max_products_per_cluster}"
                )
    return problems


def _sections(body: str) -> tuple[str, str]:
    """``(summary, evidence)`` as rendered: the lead paragraph and ``## Evidence``."""
    summary_lines: list[str] = []
    evidence_lines: list[str] = []
    target: list[str] | None = None
    for line in body.splitlines():
        if line.startswith("# "):
            target = summary_lines
            continue
        if line.startswith("## Evidence"):
            target = evidence_lines
            continue
        if line.startswith("## "):
            target = None
            continue
        if target is not None:
            target.append(line)
    return "\n".join(summary_lines).strip(), "\n".join(evidence_lines).strip()


def _non_empty(lanes: Sequence[LaneOutcome], expect: Expectations) -> list[str]:
    problems: list[str] = []
    for lane in lanes:
        for path, frontmatter in lane.products.items():
            summary, evidence = _sections(lane.bodies[path])
            if not str(frontmatter.get("title") or "").strip():
                problems.append(f"{lane.name}:{path}: empty title")
            if len(summary) < expect.min_summary_chars:
                problems.append(
                    f"{lane.name}:{path}: summary {len(summary)} chars "
                    f"< {expect.min_summary_chars}"
                )
            if not evidence:
                problems.append(f"{lane.name}:{path}: empty evidence")
    return problems


def _per_product_evidence(
    lanes: Sequence[LaneOutcome], expect: Expectations
) -> list[str]:
    """Per product, not averaged: an average hides a product with no support."""
    problems: list[str] = []
    for lane in lanes:
        for path, frontmatter in lane.products.items():
            category = str(frontmatter.get("category") or "")
            citations = len(_cited_ids(frontmatter))
            contributors = len(set(frontmatter.get("contributors") or []))
            minimum = expect.min_citations_by_category.get(category, 1)
            if citations < minimum:
                problems.append(
                    f"{lane.name}:{path}: {citations} citations < {minimum}"
                )
            floor = expect.min_contributors_by_category.get(category, 1)
            if contributors < floor:
                problems.append(
                    f"{lane.name}:{path}: {contributors} contributors < {floor}"
                )
    return problems


def _coverage(
    lanes: Sequence[LaneOutcome],
    partition: Mapping[int, Sequence[str]],
    expect: Expectations,
) -> list[str]:
    """Did the products actually consume their input, or write around it?"""
    problems: list[str] = []
    for lane in lanes:
        cited = {
            memory_id
            for frontmatter in lane.products.values()
            for memory_id in _cited_ids(frontmatter)
        }
        for cluster_id, members in partition.items():
            if not members:
                continue
            ratio = len(cited & set(members)) / len(members)
            if ratio < expect.min_citation_coverage:
                problems.append(
                    f"{lane.name}: cluster {cluster_id} coverage "
                    f"{ratio:.2f} < {expect.min_citation_coverage}"
                )
    return problems


def _unique_topics(
    lanes: Sequence[LaneOutcome], partition: Mapping[int, Sequence[str]]
) -> list[str]:
    problems: list[str] = []
    for lane in lanes:
        topics = [str(item.get("topic_id")) for item in lane.products.values()]
        duplicates = sorted({item for item in topics if topics.count(item) > 1})
        if duplicates:
            problems.append(f"{lane.name}: duplicate topic_id {duplicates}")
        for cluster_id, paths in _by_cluster(lane, partition).items():
            keys = [
                str(lane.products[path].get("canonical_topic_key")) for path in paths
            ]
            if len(set(keys)) != len(keys):
                problems.append(
                    f"{lane.name}: cluster {cluster_id} repeats a topic key"
                )
    return problems


def _declared_shape(lanes: Sequence[LaneOutcome], expect: Expectations) -> list[str]:
    problems: list[str] = []
    for lane in lanes:
        counts: dict[str, int] = {}
        for frontmatter in lane.products.values():
            category = str(frontmatter.get("category") or "")
            counts[category] = counts.get(category, 0) + 1
        if counts != expect.products_per_category:
            problems.append(
                f"{lane.name}: categories {counts} != {expect.products_per_category}"
            )
        active = lane.manifest.get("products_active") or {}
        if len(active) != sum(expect.products_per_category.values()):
            problems.append(
                f"{lane.name}: manifest lists {len(active)} active products"
            )
        if lane.manifest.get("products_tombstones"):
            problems.append(f"{lane.name}: manifest carries tombstones")
    return problems
