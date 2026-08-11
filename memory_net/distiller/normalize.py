"""Normalizing a run's artifacts so two runs can be compared (§9.2.1).

Stripping a fixed list of fields does not work here, and the reason is worth
stating: ``products_active`` is *keyed* by ``distillation_id``, which embeds the
run id, and ``citation_backrefs``, ``subject_index`` and every ``snapshot_ref``
are derived from those keys. There is no field to remove -- the identifiers have
to be remapped, everywhere they appear, before anything can be compared.

So this module defines an algorithm rather than a field list:

1. collect every ``distillation_id`` and every run id in the assets;
2. remap each to a positional name, ordered by facts that do not derive from a
   clock or a run id (category, topic key, path);
3. substitute them across the *whole* tree, keys and embedded substrings alike;
4. zero the clock fields;
5. recompute the content hash over the normalized document;
6. serialize stably.

Step 3 is the one that has to be exhaustive. A remapping that only visits the
paths someone thought of leaves a run id sitting in a nested list, and the
comparison then fails for a reason that has nothing to do with the products.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

from memory_net.shared.util.frontmatter import split_frontmatter
from memory_net.shared.util.hashing import short_hash
from memory_net.shared.util.paths import TEAM_PREFIX

PLACEHOLDER_TIMESTAMP = "1970-01-01T00:00:00Z"
PLACEHOLDER_SHA = "0" * 40
CLOCK_FIELDS = ("generated_at", "updated_at", "deleted_at", "published_at")
"""Fields whose value is a wall-clock reading and nothing else."""

SNAPSHOT_SHA_FIELDS = ("master_snapshot_sha", "config_revision")
"""Fields carrying the snapshot's commit sha, which differs per fixture setup."""

_RUN_ID_FIELDS = ("run_id", "last_run_id", "last_contribution_at")
"""Fields holding a run id. ``last_contribution_at`` reads like a timestamp and
is not one -- ``_subject_index`` stores the contributing run's id in it -- so
zeroing it as a clock field would erase a real reference."""


class NormalizationError(ValueError):
    """The assets cannot be normalized deterministically."""


@dataclass(frozen=True)
class RunAssets:
    """One run's comparable artifacts, read once and kept as data."""

    manifest: dict[str, Any]
    snapshot: dict[str, Any]
    #: repo-relative path -> file text, for every published product
    products: dict[str, str]
    metrics: dict[str, Any]

    @classmethod
    def collect(cls, repo: Path, *, run_id: str) -> RunAssets:
        """Read the four assets a run publishes.

        ``run_id`` names the per-run directory; it is not otherwise trusted --
        the normalization discovers the run ids in the data itself, because a
        run that wrote someone else's id somewhere is exactly the defect worth
        catching.
        """
        run_dir = repo / "distill_runs" / run_id
        # This run's own product snapshot, not the repository's ``team/``. The
        # latter holds only the *latest* run's publication, so comparing an
        # earlier run against a later one would read the same files for both
        # sides and report equality no matter what changed.
        products_dir = run_dir / TEAM_PREFIX
        if not products_dir.is_dir():
            raise NormalizationError(
                f"run {run_id} has no product snapshot at {products_dir}"
            )
        products = {
            f"{TEAM_PREFIX}/{path.relative_to(products_dir).as_posix()}": path.read_text(
                encoding="utf-8"
            )
            # Keyed repo-relative so the manifest's ``relative_path`` still
            # matches (that is what the content-hash restatement joins on).
            for path in sorted(products_dir.rglob("*.md"))
        }
        # The manifest as this run left it, for the same reason: the root copy
        # is overwritten by every later run.
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.is_file():
            raise NormalizationError(f"run {run_id} has no manifest at {manifest_path}")
        return cls(
            manifest=_read_json(manifest_path),
            snapshot=_read_json(run_dir / "snapshot.json"),
            products=products,
            metrics=_read_json(run_dir / "metrics.json"),
        )


def normalize(assets: RunAssets) -> dict[str, Any]:
    """The assets with every run-derived identifier and clock reading removed."""
    replacements = _replacements(assets)
    manifest = _rewrite(assets.manifest, replacements)
    snapshot = _rewrite(assets.snapshot, replacements)
    products: dict[str, Any] = {}
    for relative_path, text in assets.products.items():
        frontmatter, body = split_frontmatter(_substitute(text, replacements))
        normalized_frontmatter = _rewrite(frontmatter, replacements)
        products[_substitute(relative_path, replacements)] = {
            "frontmatter": normalized_frontmatter,
            "body": body,
            # Recomputed, not dropped: a field that is merely removed lets "the
            # text changed but the hash field was stripped" pass unnoticed.
            # Only the content hash needs it -- the evidence hash is derived
            # from citations, summary and evidence (pipeline.render_products),
            # none of which carry a clock or a run id, so it is already stable
            # across runs and recomputing it would only invent a second formula.
            #
            # Taken over the *normalized* document rather than the raw file: the
            # file still holds the generation timestamp, and hashing that would
            # make every run's hash differ for a reason the comparison is
            # supposed to have removed.
            "content_hash": short_hash(
                json.dumps(normalized_frontmatter, sort_keys=True, ensure_ascii=False)
                + body,
                16,
            ),
        }
    _restate_content_hashes(manifest, products)
    return {
        "manifest": manifest,
        "snapshot": snapshot,
        "products": products,
        # The key set, not the values: metrics are full of timings and counts
        # that legitimately differ, while a key appearing or vanishing is a
        # contract change.
        "metrics_keys": sorted(assets.metrics),
    }


def serialize(normalized: dict[str, Any]) -> str:
    """The stable text form: sorted keys, list order preserved, UTF-8 kept.

    List order is deliberately *not* sorted. The order of products, citations
    and contributors is part of what the run decided, so reordering them here
    would hide a real change.
    """
    return json.dumps(normalized, sort_keys=True, indent=2, ensure_ascii=False)


@dataclass(frozen=True)
class Difference:
    """One place two normalized assets disagree."""

    path: str
    actual: Any
    expected: Any


def diff_normalized(
    actual: dict[str, Any], expected: dict[str, Any]
) -> list[Difference]:
    """Every disagreement, with its path and both values."""
    differences: list[Difference] = []
    _diff(actual, expected, path="", into=differences)
    return differences


def assert_normalized_equal(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    """Raise with a structured diff, rather than "not equal".

    A boolean comparison of two whole artifact trees says nothing about which
    of several hundred fields moved, which is the only thing a reader needs.
    """
    differences = diff_normalized(actual, expected)
    if differences:
        rendered = "\n".join(
            f"  {item.path}: {item.actual!r} != {item.expected!r}"
            for item in differences
        )
        raise AssertionError(
            f"normalized assets differ in {len(differences)} place(s):\n{rendered}"
        )


def _replacements(assets: RunAssets) -> dict[str, str]:
    """Map every run-derived identifier to a positional stand-in.

    Ordered by ``(category, canonical_topic_key, relative_path)`` -- three facts
    that come from the products themselves. Ordering by anything clock- or
    run-derived would make the mapping differ between two runs of the same
    input, which is the one thing it must not do.
    """
    keys: dict[str, tuple[str, str, str]] = {}
    active = _mapping(assets.manifest.get("products_active"))
    for distillation_id, entry in active.items():
        keys[distillation_id] = (
            str(entry.get("category") or ""),
            str(entry.get("canonical_topic_key") or ""),
            str(entry.get("relative_path") or ""),
        )
    for distillation_id, entry in _mapping(
        assets.manifest.get("products_tombstones")
    ).items():
        keys.setdefault(
            distillation_id,
            (
                str(entry.get("last_category") or ""),
                "",
                str(entry.get("last_relative_path") or ""),
            ),
        )
    run_ids: set[str] = set()
    for relative_path, text in assets.products.items():
        frontmatter, _body = split_frontmatter(text)
        distillation_id = str(frontmatter.get("distillation_id") or "")
        if distillation_id:
            keys.setdefault(
                distillation_id,
                (
                    str(frontmatter.get("category") or ""),
                    str(frontmatter.get("canonical_topic_key") or ""),
                    relative_path,
                ),
            )
        run_ids.update(_run_ids(frontmatter))
    # Recursive, so the manifest's nested structures (subject index, tombstone
    # entries, backrefs) are covered by the same walk rather than by a list of
    # the places someone remembered.
    run_ids.update(_run_ids(assets.manifest))
    run_ids.update(_run_ids(assets.snapshot))

    ordered = sorted(keys.items(), key=lambda item: item[1])
    for (first_id, first_key), (second_id, second_key) in pairwise(ordered):
        if first_key == second_key:
            # Loud rather than arbitrary: two products with the same category,
            # topic key and path cannot be ordered by anything stable, and
            # picking one silently would make the comparison flap.
            raise NormalizationError(
                "two distillation ids share an ordering key and cannot be "
                f"remapped deterministically: {first_id}, {second_id}"
            )
    replacements = {
        distillation_id: f"distill-{index:03d}"
        for index, (distillation_id, _key) in enumerate(ordered, start=1)
    }
    replacements.update(
        {
            run_id: f"run-{index:03d}"
            for index, run_id in enumerate(sorted(run_ids), start=1)
        }
    )
    return replacements


def _run_ids(value: Any) -> set[str]:
    """Every run id in the subtree, at any depth.

    Recursive, not one level: ``subject_index`` and the tombstone entries nest
    the fields, and a run id that never enters the replacement table survives
    normalization and is then reported as a product difference it has nothing
    to do with.
    """
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _RUN_ID_FIELDS and isinstance(item, str) and item:
                found.add(item)
            else:
                found.update(_run_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_run_ids(item))
    return found


def _rewrite(value: Any, replacements: dict[str, str]) -> Any:
    """Substitute and zero, over the entire tree -- keys included."""
    if isinstance(value, dict):
        rewritten: dict[str, Any] = {}
        for key, item in value.items():
            name = _substitute(str(key), replacements)
            if name in CLOCK_FIELDS and isinstance(item, str):
                rewritten[name] = PLACEHOLDER_TIMESTAMP
            elif name in SNAPSHOT_SHA_FIELDS and isinstance(item, str):
                rewritten[name] = PLACEHOLDER_SHA
            else:
                rewritten[name] = _rewrite(item, replacements)
        return rewritten
    if isinstance(value, list):
        return [_rewrite(item, replacements) for item in value]
    if isinstance(value, str):
        return _substitute(value, replacements)
    return value


def _substitute(text: str, replacements: dict[str, str]) -> str:
    if not replacements:
        return text
    # Longest first: a distillation id contains a run id, and replacing the run
    # id first would leave the longer identifier unrecognizable.
    pattern = re.compile(
        "|".join(re.escape(key) for key in sorted(replacements, key=len, reverse=True))
    )
    return pattern.sub(lambda match: replacements[match.group()], text)


def _restate_content_hashes(manifest: dict[str, Any], products: dict[str, Any]) -> None:
    """Point the manifest's content hashes at the recomputed ones.

    The manifest hashes the product file, which embeds the run id and the
    generation time, so its recorded value cannot survive normalization. Left
    alone it would be the only field that always differs; recomputed from the
    normalized document it keeps its job of noticing a changed body.
    """
    for entry in _mapping(manifest.get("products_active")).values():
        relative_path = str(entry.get("relative_path") or "")
        product = products.get(relative_path)
        if product is not None:
            entry["content_hash"] = product["content_hash"]


def _diff(actual: Any, expected: Any, *, path: str, into: list[Difference]) -> None:
    if isinstance(actual, dict) and isinstance(expected, dict):
        for key in sorted(set(actual) | set(expected)):
            child = f"{path}.{key}" if path else str(key)
            if key not in actual or key not in expected:
                into.append(
                    Difference(
                        path=child,
                        actual=actual.get(key, "<absent>"),
                        expected=expected.get(key, "<absent>"),
                    )
                )
                continue
            _diff(actual[key], expected[key], path=child, into=into)
        return
    if isinstance(actual, list) and isinstance(expected, list):
        if len(actual) != len(expected):
            into.append(Difference(path=f"{path}[]", actual=actual, expected=expected))
            return
        for index, (left, right) in enumerate(zip(actual, expected, strict=True)):
            _diff(left, right, path=f"{path}[{index}]", into=into)
        return
    if actual != expected:
        into.append(Difference(path=path or "<root>", actual=actual, expected=expected))


def _mapping(value: Any) -> dict[str, Any]:
    return (
        {str(key): item for key, item in value.items()}
        if isinstance(value, dict)
        else {}
    )


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}
