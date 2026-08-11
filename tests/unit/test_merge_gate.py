"""The merge gate re-derives its verdict from the branch, not from the run.

Every test builds a release tree by hand and asks the gate what it thinks. That
is deliberate: the gate exists precisely for the case where the run that wrote
the branch cannot be trusted, so it must never consult that run's own report.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from memory_net.distiller.merge_gate import main, validate_release_tree

PRODUCT = """---
schema_version: memory-net/team-product/v1
distillation_id: distill-run-001
title: Retry policy
category: common_knowledge
topic_id: topic-v1-retry-policy
contributors:
- alice
citations:
- memory_id: leaf-a-mem-1
  revision_hash: rev00001
  author_node: alice
---

# Retry policy

Use backoff.
"""


def _tree(tmp_path: Path, *, product: str | None = None, **manifest: Any) -> Path:
    root = tmp_path / "release"
    (root / "team" / "common_knowledge").mkdir(parents=True)
    (root / "team" / "common_knowledge" / "retry-policy.md").write_text(
        PRODUCT if product is None else product, encoding="utf-8"
    )
    payload: dict[str, Any] = {
        "schema_version": "memory-net/master-manifest/v1",
        "compatible_leaf_range": ">=0.1.0,<0.2.0",
        "products_active": {
            "distill-run-001": {
                "relative_path": "team/common_knowledge/retry-policy.md",
                "distillation_id": "distill-run-001",
            }
        },
    }
    payload.update(manifest)
    (root / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    return root


def test_a_well_formed_release_tree_passes(tmp_path: Path) -> None:
    assert validate_release_tree(_tree(tmp_path)) == []


def test_a_missing_manifest_is_refused(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    (root / "manifest.json").unlink()

    failures = validate_release_tree(root)

    assert any("manifest.json: missing" in failure for failure in failures)


def test_a_product_with_a_wrong_schema_version_is_refused(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        product=PRODUCT.replace("team-product/v1", "team-product/v99"),
    )

    failures = validate_release_tree(root)

    assert any("schema" in failure for failure in failures)


def test_a_product_without_citations_is_refused(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        product=PRODUCT.split("citations:")[0] + "citations: []\n---\n\n# Retry\n",
    )

    failures = validate_release_tree(root)

    assert any("must cite its evidence" in failure for failure in failures)


def test_a_citation_missing_its_revision_is_refused(tmp_path: Path) -> None:
    root = _tree(tmp_path, product=PRODUCT.replace("  revision_hash: rev00001\n", ""))

    failures = validate_release_tree(root)

    assert any("missing revision_hash" in failure for failure in failures)


def test_an_evidence_rule_from_the_profile_is_re_applied(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    (root / "config").mkdir()
    (root / "config" / "distill.yaml").write_text(
        "category_evidence_rules:\n"
        "  common_knowledge:\n"
        "    min_contributors: 2\n",
        encoding="utf-8",
    )

    failures = validate_release_tree(root)

    assert any("contributor(s) below" in failure for failure in failures)


def test_a_product_the_manifest_does_not_name_is_refused(tmp_path: Path) -> None:
    root = _tree(tmp_path, products_active={})

    failures = validate_release_tree(root)

    assert any("absent from manifest.json" in failure for failure in failures)


def test_a_manifest_entry_with_no_file_is_refused(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        products_active={
            "distill-run-001": {
                "relative_path": "team/common_knowledge/retry-policy.md",
                "distillation_id": "distill-run-001",
            },
            "distill-run-002": {
                "relative_path": "team/common_knowledge/ghost.md",
                "distillation_id": "distill-run-002",
            },
        },
    )

    failures = validate_release_tree(root)

    assert any("not on the branch" in failure for failure in failures)


def test_a_tampered_review_request_is_refused(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    request = root / "distill_runs" / "run-1" / "review_request.json"
    request.parent.mkdir(parents=True)
    request.write_text(
        json.dumps({"schema_version": "memory-net/review-request/v1"}),
        encoding="utf-8",
    )

    failures = validate_release_tree(root)

    assert any("review_request.json" in failure for failure in failures)


def test_the_entry_point_reports_its_verdict_through_the_exit_code(
    tmp_path: Path,
) -> None:
    root = _tree(tmp_path)

    assert main([str(root)]) == 0

    (root / "manifest.json").unlink()
    assert main([str(root)]) == 1
