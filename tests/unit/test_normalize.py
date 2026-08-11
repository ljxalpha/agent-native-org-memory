"""The normalization algorithm and its comparator (§9.2.1).

The assets under test are built by the real renderer and the real manifest
builder, not hand-written: the whole point is that the identifiers this project
mints are tangled through the manifest's keys, its backrefs and its subject
index, and a hand-made fixture would only contain the tangles someone
remembered.

The negative cases carry the weight. An algorithm that normalizes away a real
difference looks exactly like a correct one until something regresses.
"""

from __future__ import annotations

import json
from itertools import chain
from pathlib import Path
from typing import Any

import pytest

from memory_net.distiller.normalize import (
    NormalizationError,
    RunAssets,
    assert_normalized_equal,
    diff_normalized,
    normalize,
    serialize,
)
from memory_net.distiller.orchestrator import DistillOrchestrator
from memory_net.distiller.pipeline import render_products
from memory_net.distiller.runctx import RunContextBuilder, RunIdentity

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

PARSED: dict[str, Any] = {
    "products": [
        {
            "title": "Retry token refresh once",
            "category": "best_practice",
            "summary": "Retry once with jitter before failing.",
            "evidence": "Both leaves saw the same failure.",
            "citations": [{"memory_id": "leaf-a-mem-1", "revision_hash": "aaa111"}],
        },
        {
            "title": "Deploy inside the window",
            "category": "best_practice",
            "summary": "Deploy between 02:00 and 04:00 UTC.",
            "evidence": "Two incidents outside the window.",
            "citations": [{"memory_id": "leaf-b-mem-1", "revision_hash": "bbb222"}],
        },
    ]
}


def _assets(
    *, run_id: str, generated_at: str, snapshot_sha: str, parsed: Any = None
) -> RunAssets:
    """One run's artifacts, produced by the real renderer and manifest builder."""
    products = render_products(
        parsed or PARSED,
        RECORDS,
        run_id=run_id,
        start_index=1,
        min_citations_by_category={},
        min_contributors_by_category={},
        allowed_categories=None,
        seen_topics=set(),
        now=lambda: generated_at,
    )
    context = (
        RunContextBuilder()
        .set_identity(RunIdentity(run_id=run_id, now=lambda: generated_at))
        .freeze()
    )
    orchestrator = DistillOrchestrator(
        repo_path=Path("/nonexistent"), pipeline=None, context=context  # type: ignore[arg-type]
    )
    manifest = orchestrator._master_manifest(
        run_id=run_id,
        snapshot_sha=snapshot_sha,
        products=products,
        previous_manifest={},
    )
    return RunAssets(
        manifest=manifest,
        snapshot={
            "leaf_refs": {"leaf/alice-claude-01": "a" * 40},
            "master_snapshot_sha": snapshot_sha,
            "run_id": run_id,
        },
        products={
            product.relative_path.as_posix(): product.content for product in products
        },
        metrics={"run_id": run_id, "products": len(products), "status": "success"},
    )


def _first_run(parsed: Any = None) -> RunAssets:
    return _assets(
        run_id="20260810T101010Z-a1b2c3d4",
        generated_at="2026-08-10T10:10:10Z",
        snapshot_sha="a" * 40,
        parsed=parsed,
    )


def _second_run(parsed: Any = None) -> RunAssets:
    return _assets(
        run_id="20260811T222222Z-99887766",
        generated_at="2026-08-11T22:22:22Z",
        snapshot_sha="b" * 40,
        parsed=parsed,
    )


# --------------------------------------------------------------------------
# The positive case
# --------------------------------------------------------------------------


def test_two_runs_of_the_same_input_normalize_equal() -> None:
    assert_normalized_equal(normalize(_first_run()), normalize(_second_run()))


def test_the_serialized_form_is_byte_identical() -> None:
    assert serialize(normalize(_first_run())) == serialize(normalize(_second_run()))


def test_no_run_id_survives_normalization() -> None:
    """The check that says the walk reached everywhere, without naming fields."""
    text = serialize(normalize(_first_run()))

    assert "20260810T101010Z" not in text
    assert "2026-08-10T10:10:10Z" not in text
    assert '"master_snapshot_sha": "' + "0" * 40 in text


def test_the_identifiers_are_remapped_not_removed() -> None:
    normalized = normalize(_first_run())

    assert sorted(normalized["manifest"]["products_active"]) == [
        "distill-001",
        "distill-002",
    ]
    backrefs = normalized["manifest"]["citation_backrefs"]
    assert sorted(chain.from_iterable(backrefs.values())) == [
        "distill-001",
        "distill-002",
    ]


# --------------------------------------------------------------------------
# The negative cases: leaving something out has to fail
# --------------------------------------------------------------------------


def test_a_changed_word_in_a_product_body_fails_the_comparison() -> None:
    """What the recomputed content hash is for."""
    first = normalize(_first_run())
    second_assets = _second_run()
    path, text = next(iter(second_assets.products.items()))
    second_assets.products[path] = text.replace("jitter", "backoff")

    differences = diff_normalized(first, normalize(second_assets))

    assert differences
    assert any("content_hash" in item.path for item in differences)


def test_a_missed_backref_remapping_fails_the_comparison() -> None:
    """Proves the whole-tree walk is load-bearing: leave one nested identifier
    behind and the comparison must notice."""
    first = normalize(_first_run())
    second = normalize(_second_run())
    stale = "20260811T222222Z-99887766"
    key = next(iter(second["manifest"]["citation_backrefs"]))
    second["manifest"]["citation_backrefs"][key] = [f"distill-{stale}-001"]

    with pytest.raises(AssertionError, match="citation_backrefs"):
        assert_normalized_equal(second, first)


def test_reordering_a_products_citations_fails_the_comparison() -> None:
    """List order is semantics, not presentation, so it is never sorted away.

    The order the *products* were emitted in is a different matter: the remap is
    ordered by category, topic key and path precisely so that it does not depend
    on emission order, and two runs that emitted the same products in a
    different order really did produce the same artifacts.
    """
    both = _parsed_with_both_citations()
    reversed_citations = json.loads(json.dumps(both))
    reversed_citations["products"][0]["citations"].reverse()

    differences = diff_normalized(
        normalize(_first_run(both)), normalize(_second_run(reversed_citations))
    )

    assert differences
    assert any("citations" in item.path for item in differences)


def test_emission_order_alone_is_not_a_difference() -> None:
    """The complement of the case above, stated so the boundary is explicit."""
    swapped = dict(PARSED)
    swapped["products"] = list(reversed(PARSED["products"]))

    assert_normalized_equal(normalize(_first_run()), normalize(_second_run(swapped)))


def _parsed_with_both_citations() -> dict[str, Any]:
    both = json.loads(json.dumps(PARSED))
    both["products"][0]["citations"] = [
        {"memory_id": "leaf-a-mem-1", "revision_hash": "aaa111"},
        {"memory_id": "leaf-b-mem-1", "revision_hash": "bbb222"},
    ]
    return both


def test_the_old_strip_the_fields_approach_would_have_passed() -> None:
    """The control case. Removing the volatile fields leaves the remapped
    identifiers untouched, so a stale one compares equal to a fresh one -- which
    is why this module remaps instead of stripping."""
    first = _strip_volatile_fields(normalize(_first_run()))
    second = normalize(_second_run())
    key = next(iter(second["manifest"]["citation_backrefs"]))
    second["manifest"]["citation_backrefs"][key] = ["<stripped>"]

    assert not diff_normalized(
        _strip_volatile_fields(second)["manifest"]["citation_backrefs"],
        first["manifest"]["citation_backrefs"],
    )


def _strip_volatile_fields(normalized: dict[str, Any]) -> dict[str, Any]:
    """The rejected algorithm: blank out a known list of fields."""
    stripped = json.loads(json.dumps(normalized))
    stripped["manifest"]["citation_backrefs"] = {
        key: ["<stripped>"] for key in stripped["manifest"]["citation_backrefs"]
    }
    return stripped


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_an_ambiguous_ordering_key_is_refused() -> None:
    """Two products that cannot be ordered by stable facts stop the comparison
    instead of ordering them arbitrarily and flapping."""
    assets = _first_run()
    active = assets.manifest["products_active"]
    first_id, second_id = sorted(active)
    for field in ("category", "canonical_topic_key", "relative_path"):
        active[second_id][field] = active[first_id][field]

    with pytest.raises(NormalizationError, match="ordering key"):
        normalize(assets)


def test_collect_reads_the_four_assets(tmp_path: Path) -> None:
    run_id = "20260810T101010Z-a1b2c3d4"
    assets = _first_run()
    (tmp_path / "distill_runs" / run_id).mkdir(parents=True)
    (tmp_path / "manifest.json").write_text(
        json.dumps(assets.manifest), encoding="utf-8"
    )
    (tmp_path / "distill_runs" / run_id / "snapshot.json").write_text(
        json.dumps(assets.snapshot), encoding="utf-8"
    )
    (tmp_path / "distill_runs" / run_id / "metrics.json").write_text(
        json.dumps(assets.metrics), encoding="utf-8"
    )
    for relative_path, text in assets.products.items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    collected = RunAssets.collect(tmp_path, run_id=run_id)

    assert_normalized_equal(normalize(collected), normalize(assets))
