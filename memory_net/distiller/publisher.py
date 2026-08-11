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

import json
import os
import re
import shlex
import signal
import subprocess
from collections.abc import Mapping
from pathlib import Path
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


PR_COMMAND_ENV = "AOMT_PR_COMMAND"
DEFAULT_PR_COMMAND = "gh pr create"
PR_ENV_ALLOWLIST_ENV = "AOMT_PR_ENV_ALLOWLIST"
_TEARDOWN_GRACE_S = 5.0


def open_pull_request(
    request_path: Path,
    *,
    cwd: Path,
    command: str | None = None,
    timeout_s: float = 60.0,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Ask an external command to open the pull request this run asked for.

    Every field comes from ``review_request.json`` -- head, base, title and the
    body itself, passed as ``--body-file`` rather than re-rendered. The artifact
    is the single source: a body assembled here could disagree with the file the
    reviewer is looking at, and it would be text nothing validated.

    The subprocess shape is the hardened one used for the agent bridge:
    ``shlex.split`` (never a shell), its own session so the whole process group
    can be signalled, a timeout, and ``SIGTERM`` -> bounded wait -> ``SIGKILL``
    on the group. A ``gh`` that hangs on an auth prompt otherwise holds the run
    open forever, and a child it spawned outlives it.

    The child's environment is *built*, not inherited: this process holds an LLM
    API key, and a PR command has no business seeing it. Only variables the
    operator names in ``AOMT_PR_ENV_ALLOWLIST`` are forwarded, by key.
    """
    environ = os.environ if environ is None else environ
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    validate_review_request(payload)
    argv = [
        *shlex.split(command or environ.get(PR_COMMAND_ENV) or DEFAULT_PR_COMMAND),
        "--head",
        payload["head"],
        "--base",
        payload["base"],
        "--title",
        payload["title"],
        "--body-file",
        str(request_path),
    ]
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(cwd),
        env=pr_environment(environ),
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        stderr = _teardown(proc)
        raise PullRequestError(
            f"pull request command timed out after {timeout_s}s"
            f"{_excerpt('stderr', stderr)}"
        ) from None
    except BaseException:
        # Any other way out of this frame must not leave the command running.
        _teardown(proc)
        raise
    if proc.returncode != 0:
        raise PullRequestError(
            f"pull request command failed with exit code {proc.returncode}"
            f"{_excerpt('stdout', stdout)}{_excerpt('stderr', stderr)}"
        )
    url = stdout.strip()
    if not url.startswith("https://"):
        # The command's contract is to print the pull request URL. Anything
        # else means it did something other than what was asked, and reporting
        # success on it would record a review gate that may not exist.
        raise PullRequestError(
            "pull request command printed no URL"
            f"{_excerpt('stdout', stdout)}{_excerpt('stderr', stderr)}"
        )
    return url


def pr_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """The child's environment: PATH, plus whatever the operator allowlisted.

    Deliberately not ``os.environ``. Names are copied, values are never read
    here for any purpose other than being handed over, and nothing is forwarded
    that the operator did not name.
    """
    env = {
        "PATH": environ.get("PATH", ""),
        # A credential helper that decides to prompt would otherwise block until
        # the timeout, and the reviewer would see nothing at all.
        "GIT_TERMINAL_PROMPT": "0",
    }
    for name in (environ.get(PR_ENV_ALLOWLIST_ENV) or "").split(","):
        key = name.strip()
        if key and key in environ:
            env[key] = environ[key]
    return env


class PullRequestError(RuntimeError):
    """The PR command failed, timed out, or did not do what it was asked."""


def _teardown(proc: subprocess.Popen[str]) -> str:
    """Signal the command's whole process group, then drain what it wrote."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        pgid = None
    if pgid is not None:
        _signal_group(pgid, signal.SIGTERM)
        _wait_briefly(proc)
        # Unconditional: the command may exit on SIGTERM while a child it
        # spawned ignores it, and that child is what this exists to reap.
        _signal_group(pgid, signal.SIGKILL)
    _wait_briefly(proc)
    try:
        _, stderr = proc.communicate(timeout=_TEARDOWN_GRACE_S)
    except (subprocess.TimeoutExpired, ValueError):
        return ""
    return stderr or ""


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        return


def _wait_briefly(proc: subprocess.Popen[str]) -> None:
    try:
        proc.wait(timeout=_TEARDOWN_GRACE_S)
    except subprocess.TimeoutExpired:
        return


def _excerpt(label: str, text: str) -> str:
    value = (text or "").strip()
    return f"; {label}={value[:400]}" if value else ""
