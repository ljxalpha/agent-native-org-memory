"""The review request a release branch carries, and its allowlist (M2-004).

``review_request.json`` states *what to review and where to look*: the branch,
the base, the run, the snapshot the run read, a pointer to the metrics, and how
many products of which categories are up for review. It carries no product text
at all -- a reviewer reads the products from the branch, and a summary file that
quoted them would be a second copy of team knowledge travelling with different
retention and different access than the branch it describes.

Both the key set *and* the values are constrained. An allowlist of key names
alone would still let ``head`` be an arbitrary string, and that string is handed
to a PR command as an argument.
"""

from __future__ import annotations

import re
from typing import Any

SCHEMA_VERSION = "memory-net/review-request/v1"

REVIEW_GATE_STATUSES = ("disabled", "pending", "approved", "rejected")
"""The full value domain of ``metrics.json``'s ``review_gate_status``.

``disabled`` and ``pending`` are written by the distiller. ``approved`` and
``rejected`` are written back by the merge gate, which is the only party that
can know either -- so they exist here as a vocabulary the distiller accepts, not
as states it can decide for itself.
"""

FIELDS = (
    "schema_version",
    "run_id",
    "head",
    "base",
    "title",
    "codeowners_path",
    "master_snapshot_sha",
    "metrics_path",
    "products_count",
    "categories",
)
"""Every key a review request may carry. One more, and validation fails."""

_REF = re.compile(r"^[A-Za-z0-9._/-]+$")
_TITLE = "distill run {run_id}"


class ReviewRequestError(ValueError):
    """A review request that must not be written, or must not be trusted."""


def build_review_request(
    *,
    run_id: str,
    head: str,
    base: str,
    codeowners_path: str,
    master_snapshot_sha: str,
    products_count: int,
    categories: list[str],
    allowed_categories: set[str] | None = None,
) -> dict[str, Any]:
    """Assemble the request, then validate it like any untrusted input.

    Validated on the way *out* as well as on the way in: this is the file the PR
    body is built from, so the moment it is written is the last one at which a
    malformed field costs nothing.
    """
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "head": head,
        "base": base,
        "title": _TITLE.format(run_id=run_id),
        "codeowners_path": codeowners_path,
        "master_snapshot_sha": master_snapshot_sha,
        "metrics_path": f"distill_runs/{run_id}/metrics.json",
        "products_count": int(products_count),
        "categories": sorted(categories),
    }
    validate_review_request(payload, allowed_categories=allowed_categories)
    return payload


def validate_review_request(
    payload: Any,
    *,
    allowed_categories: set[str] | None = None,
) -> None:
    """Refuse anything outside the allowlist, in keys and in values."""
    if not isinstance(payload, dict):
        raise ReviewRequestError("review request must be an object")
    unknown = sorted(set(payload) - set(FIELDS))
    if unknown:
        raise ReviewRequestError(f"review request has unknown keys: {unknown}")
    missing = sorted(set(FIELDS) - set(payload))
    if missing:
        raise ReviewRequestError(f"review request is missing keys: {missing}")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise ReviewRequestError(
            f"review request schema must be {SCHEMA_VERSION}: "
            f"{payload['schema_version']!r}"
        )
    run_id = _checked_str(payload, "run_id")
    for key in ("head", "base"):
        value = _checked_str(payload, key)
        if not _REF.match(value):
            raise ReviewRequestError(f"{key} is not a branch name: {value!r}")
    if payload["title"] != _TITLE.format(run_id=run_id):
        # A free-text title is a place for model-derived text to reach a PR
        # without ever having been scanned as output.
        raise ReviewRequestError("title must be derived from the run id")
    _checked_repo_relative(payload, "codeowners_path")
    _checked_repo_relative(payload, "metrics_path")
    _checked_str(payload, "master_snapshot_sha")
    count = payload["products_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ReviewRequestError(f"products_count must be a count: {count!r}")
    categories = payload["categories"]
    if not isinstance(categories, list) or any(
        not isinstance(item, str) for item in categories
    ):
        raise ReviewRequestError("categories must be a list of names")
    if allowed_categories is not None and not set(categories) <= allowed_categories:
        raise ReviewRequestError(
            "categories must come from the configured set: "
            f"{sorted(set(categories) - allowed_categories)}"
        )


def _checked_str(payload: dict[str, Any], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str) or not value.strip():
        raise ReviewRequestError(f"{key} must be a non-empty string: {value!r}")
    return value


def _checked_repo_relative(payload: dict[str, Any], key: str) -> str:
    value = _checked_str(payload, key)
    if value.startswith("/") or ".." in value.split("/"):
        raise ReviewRequestError(f"{key} must stay inside the repo: {value!r}")
    return value
