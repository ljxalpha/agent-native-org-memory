"""Re-validate a release branch before it is merged into master (M2-004).

This runs on the *branch*, not on the run that produced it, and it re-derives
its verdict from the files alone. That is the point: the distiller already
checked these things, but the merge gate has to hold even if the run that
pushed the branch was buggy, was replayed, or was not this code at all. A gate
that trusts the producer's own report is not a gate.

Three checks, matching the three the distiller applies before publishing:

* **schema** -- every product carries a valid team-product frontmatter, and the
  manifest is a valid master manifest;
* **citations** -- every product cites something, every citation names a memory
  and a revision, and the manifest and the tree agree about which products
  exist;
* **evidence rules** -- the per-category ``min_citations`` and
  ``min_contributors`` thresholds from the profile still hold.

Zero credentials, zero network, zero writes: it reads a checkout and returns a
list of failures, which is what lets it stay green on a fork pull request.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from memory_net.distiller.categories import canonical_category
from memory_net.distiller.publisher import ReviewRequestError, validate_review_request
from memory_net.shared.schema import SchemaError
from memory_net.shared.schema.validation import (
    validate_master_manifest,
    validate_team_product_frontmatter,
)
from memory_net.shared.util.frontmatter import split_frontmatter

TEAM_PREFIX = "team"
DEFAULT_PROFILE = "config/distill.yaml"


def validate_release_tree(
    root: Path,
    *,
    profile: str = DEFAULT_PROFILE,
) -> list[str]:
    """Every reason this tree must not be merged, or an empty list."""
    failures: list[str] = []
    manifest = _load_manifest(root, failures)
    rules = _evidence_rules(root / profile)
    products: dict[str, dict[str, Any]] = {}
    for path in sorted((root / TEAM_PREFIX).rglob("*.md")):
        relative = path.relative_to(root).as_posix()
        try:
            frontmatter, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            failures.append(f"{relative}: unreadable product ({type(exc).__name__})")
            continue
        try:
            validate_team_product_frontmatter(frontmatter)
        except SchemaError as exc:
            failures.append(f"{relative}: schema: {exc}")
            continue
        products[relative] = frontmatter
        failures.extend(_citation_failures(relative, frontmatter))
        failures.extend(_evidence_failures(relative, frontmatter, rules))
    if manifest is not None:
        failures.extend(_manifest_failures(manifest, products))
    failures.extend(_review_request_failures(root))
    return failures


def _load_manifest(root: Path, failures: list[str]) -> dict[str, Any] | None:
    path = root / "manifest.json"
    if not path.exists():
        failures.append("manifest.json: missing from the release branch")
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        failures.append(f"manifest.json: unreadable ({type(exc).__name__})")
        return None
    try:
        validate_master_manifest(manifest)
    except SchemaError as exc:
        failures.append(f"manifest.json: schema: {exc}")
        return None
    return manifest if isinstance(manifest, dict) else None


def _citation_failures(relative: str, frontmatter: dict[str, Any]) -> list[str]:
    citations = frontmatter.get("citations") or []
    if not citations:
        return [f"{relative}: citations: a product must cite its evidence"]
    failures: list[str] = []
    for index, citation in enumerate(citations):
        if not isinstance(citation, dict):
            failures.append(f"{relative}: citations[{index}]: not an object")
            continue
        for key in ("memory_id", "revision_hash"):
            value = citation.get(key)
            if not isinstance(value, str) or not value.strip():
                failures.append(f"{relative}: citations[{index}]: missing {key}")
    return failures


def _evidence_failures(
    relative: str,
    frontmatter: dict[str, Any],
    rules: dict[str, dict[str, int]],
) -> list[str]:
    category = canonical_category(str(frontmatter.get("category") or ""))
    rule = rules.get(category, {})
    failures: list[str] = []
    citations = [
        item for item in frontmatter.get("citations") or [] if isinstance(item, dict)
    ]
    min_citations = rule.get("min_citations", 1)
    if len(citations) < min_citations:
        failures.append(
            f"{relative}: evidence rule: {len(citations)} citation(s) below "
            f"{category} minimum {min_citations}"
        )
    contributors = {
        str(item.get("author_node"))
        for item in citations
        if str(item.get("author_node") or "")
    }
    min_contributors = rule.get("min_contributors", 1)
    if len(contributors) < min_contributors:
        failures.append(
            f"{relative}: evidence rule: {len(contributors)} contributor(s) below "
            f"{category} minimum {min_contributors}"
        )
    return failures


def _manifest_failures(
    manifest: dict[str, Any],
    products: dict[str, dict[str, Any]],
) -> list[str]:
    """The manifest and the tree must describe the same set of products.

    A manifest naming a file that is not there publishes a dangling reference;
    a file the manifest does not name is a product that reached the branch
    without going through the run that wrote the manifest.
    """
    active = manifest.get("products_active")
    active = active if isinstance(active, dict) else {}
    listed = {
        str(entry.get("relative_path"))
        for entry in active.values()
        if isinstance(entry, dict)
    }
    failures = [
        f"manifest.json: names a product that is not on the branch: {path}"
        for path in sorted(listed - set(products))
    ]
    failures.extend(
        f"{path}: on the branch but absent from manifest.json"
        for path in sorted(set(products) - listed)
    )
    return failures


def _review_request_failures(root: Path) -> list[str]:
    """A review request, if one is present, must still be within its allowlist."""
    failures: list[str] = []
    for path in sorted((root / "distill_runs").glob("*/review_request.json")):
        relative = path.relative_to(root).as_posix()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(f"{relative}: unreadable ({type(exc).__name__})")
            continue
        try:
            validate_review_request(payload)
        except ReviewRequestError as exc:
            failures.append(f"{relative}: {exc}")
    return failures


def _evidence_rules(profile: Path) -> dict[str, dict[str, int]]:
    """The per-category thresholds, or nothing when the profile is absent.

    Absent is not an error here: the merge gate runs against a memory
    repository, whose profile lives at whatever path that repository chose. The
    per-category default of one citation and one contributor still applies.
    """
    if not profile.exists():
        return {}
    config = yaml.safe_load(profile.read_text(encoding="utf-8")) or {}
    raw = config.get("category_evidence_rules")
    if not isinstance(raw, dict):
        return {}
    rules: dict[str, dict[str, int]] = {}
    for category, value in raw.items():
        if not isinstance(value, dict):
            continue
        rule: dict[str, int] = {}
        for key in ("min_citations", "min_contributors"):
            if isinstance(value.get(key), int) and not isinstance(value[key], bool):
                rule[key] = int(value[key])
        rules[canonical_category(str(category))] = rule
    return rules


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]) if args else Path.cwd()
    profile = args[1] if len(args) > 1 else DEFAULT_PROFILE
    failures = validate_release_tree(root, profile=profile)
    for failure in failures:
        print(f"[ABORT] {failure}", file=sys.stderr)
    if failures:
        print(f"merge gate refused {len(failures)} finding(s)", file=sys.stderr)
        return 1
    print("merge gate: schema, citations and evidence rules all hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
