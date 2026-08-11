#!/usr/bin/env python3
"""Build the hermetic golden input repository from the assets beside this file.

A recorded bundle only covers the LLM calls. Replaying it still fetches the
remote, reads the leaf branches, clusters the records and assembles the
prompts -- so an input repository that differs between two machines produces
different prompts, and strict replay misses on every call. This builder exists
to make that input reproducible commit-for-commit: fixed identities, fixed
commit dates, fixed file modes, fixed write order, and record-derived values
(``revision_hash``, ``report_fingerprint``) that contain no clock.

Nothing built here is committed to the repository. ``--out`` is a scratch
directory:

    build_repo.py --out /tmp/golden_repo
    memory-net-distiller run --repo /tmp/golden_repo ...

The bare remote lands next to it as ``<out>.remote.git``. A working clone and a
bare repository cannot be the same directory, and putting the remote *inside*
``--out`` would make it part of the tree being distilled; a sibling keeps
``--repo`` and ``--out`` the same value.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # pragma: no cover - direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from memory_net.distiller.output_scanner import Ruleset
from memory_net.shared.util.frontmatter import render_frontmatter
from memory_net.shared.util.hashing import revision_hash, short_hash, stable_json

ASSETS = Path(__file__).resolve().parent

PROFILE_PATH = "config/distill.deepseek.yaml"
"""Same repo-relative path as the shipped production profile (§9.10)."""

FIXTURE_EPOCH = "2026-01-01T00:00:00+00:00"
"""The one commit timestamp. Every commit this builder makes uses it, because a
commit sha covers its own dates: a wall clock here would change every sha."""

FIXTURE_IDENTITY = {
    "GIT_AUTHOR_NAME": "aomt-fixture",
    "GIT_AUTHOR_EMAIL": "aomt-fixture@example.invalid",
    "GIT_COMMITTER_NAME": "aomt-fixture",
    "GIT_COMMITTER_EMAIL": "aomt-fixture@example.invalid",
    "GIT_AUTHOR_DATE": FIXTURE_EPOCH,
    "GIT_COMMITTER_DATE": FIXTURE_EPOCH,
}

LEAF_NAMES = ("auth_retry", "deploy_window", "search_index")
"""Ordered, not globbed: the branch creation order is part of what is being
kept reproducible, and a glob orders by whatever the filesystem returns."""


@dataclass(frozen=True)
class BuildResult:
    """Where the repository landed, and the shas that must not drift."""

    work: Path
    remote: Path
    master_sha: str
    leaf_shas: dict[str, str]


def build(out: Path, *, remote: Path | None = None) -> BuildResult:
    """Create the bare remote and a working clone, and return their shas."""
    out = out.resolve()
    remote = (remote or out.with_name(out.name + ".remote.git")).resolve()
    if remote == out:
        raise ValueError("the bare remote cannot be the working clone")
    for path in (out, remote):
        if path.exists():
            shutil.rmtree(path)
    remote.mkdir(parents=True)
    _git(remote, "init", "--bare", "-b", "master", "--quiet", ".")

    staging = out.with_name(out.name + ".staging")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    _git(staging, "init", "-b", "master", "--quiet", ".")
    _git(staging, "remote", "add", "origin", str(remote))

    master_sha = _commit_master(staging)
    leaf_shas = {name: _commit_leaf(staging, name) for name in LEAF_NAMES}
    shutil.rmtree(staging)

    _git(out.parent, "clone", "--quiet", str(remote), str(out))
    # Local identity only: a fixture clone must never depend on, or fall back
    # to, whatever identity the machine running it happens to have configured.
    _git(out, "config", "user.name", FIXTURE_IDENTITY["GIT_AUTHOR_NAME"])
    _git(out, "config", "user.email", FIXTURE_IDENTITY["GIT_AUTHOR_EMAIL"])
    result = BuildResult(
        work=out, remote=remote, master_sha=master_sha, leaf_shas=leaf_shas
    )
    preflight(result)
    return result


def preflight(result: BuildResult) -> None:
    """Load, offline, everything the profile points at inside the snapshot.

    The profile names repo-relative resources -- both prompt templates and the
    output scanner's rule file -- and every one of them is read through
    ``git show`` from the frozen snapshot. A missing one is only discovered
    when the run reaches it, which for the scanner rules is after the first
    billed call. Checking here costs nothing and fails before any of that.
    """
    import yaml  # imported here: only this check needs it

    profile_text = _show(result.work, result.master_sha, PROFILE_PATH)
    profile = yaml.safe_load(profile_text) or {}
    if not isinstance(profile, dict):
        raise TypeError(f"{PROFILE_PATH} is not a mapping")

    scanner = profile.get("output_scanner") or {}
    patterns_path = str(scanner.get("patterns_path") or "")
    if not patterns_path:
        raise RuntimeError("profile declares no output_scanner.patterns_path")
    # Constructed, not merely read: a rule file that parses as YAML but not as a
    # ruleset would still fail at the first scan.
    Ruleset.from_text(
        _show(result.work, result.master_sha, patterns_path),
        dynamic_values={"local_hostname": ["fixture-host"]},
    )

    prompts = profile.get("prompts") or {}
    for key in ("system_template_path", "user_template_path"):
        rel = str(prompts.get(key) or "")
        if not rel:
            raise RuntimeError(f"profile declares no prompts.{key}")
        if not _show(result.work, result.master_sha, rel).strip():
            raise RuntimeError(f"empty prompt template in snapshot: {rel}")


def _commit_master(staging: Path) -> str:
    """Master: the profile, its prompts, its rules, and the empty manifest."""
    _write(staging / "bootstrap_state.json", (ASSETS / "bootstrap.json").read_bytes())
    _write(staging / "manifest.json", (ASSETS / "manifest.json").read_bytes())
    for rel in (
        PROFILE_PATH,
        "config/redaction_patterns.yaml",
        "prompts/distill_system.md",
        "prompts/distill_user.md",
    ):
        _write(staging / rel, (ASSETS / rel).read_bytes())
    _write(staging / "team/.gitkeep", b"")
    _write(staging / "distill_runs/.gitkeep", b"")
    _git(staging, "add", "--all")
    _git(staging, "commit", "--quiet", "-m", "golden fixture: master")
    _git(staging, "push", "--quiet", "origin", "master")
    return _git(staging, "rev-parse", "HEAD").strip()


def _commit_leaf(staging: Path, name: str) -> str:
    """One leaf branch, built from ``leaves/<name>.json``.

    Orphaned from master on purpose: a leaf branch in this system carries only
    its own reported memories, and inheriting master's tree would put the
    profile and the prompts on a branch the distiller reads records from.
    """
    leaf = json.loads((ASSETS / "leaves" / f"{name}.json").read_text("utf-8"))
    branch = str(leaf["branch"])
    _git(staging, "checkout", "--quiet", "--orphan", f"tmp-{name}")
    _git(staging, "rm", "-rq", "--cached", "-r", ".")
    for path in sorted(staging.iterdir()):
        if path.name == ".git":
            continue
        shutil.rmtree(path) if path.is_dir() else path.unlink()

    entries: dict[str, Any] = {}
    for record in leaf["records"]:
        entry, relative_path, content = _reported_record(leaf, record)
        entries[str(record["memory_id"])] = entry
        _write(staging / relative_path, content.encode("utf-8"))
    manifest = {
        "schema_version": "memory-net/leaf-manifest/v1",
        "node_id": str(leaf["node_id"]),
        "entries": entries,
    }
    _write(staging / "manifest.json", _json_bytes(manifest))
    _write(staging / ".health.json", _json_bytes(_leaf_health(leaf)))
    _git(staging, "add", "--all")
    _git(staging, "commit", "--quiet", "-m", f"golden fixture: {branch}")
    _git(staging, "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}")
    sha = _git(staging, "rev-parse", "HEAD").strip()
    _git(staging, "checkout", "--quiet", "master")
    _git(staging, "branch", "-qD", f"tmp-{name}")
    return sha


def _reported_record(
    leaf: dict[str, Any], record: dict[str, Any]
) -> tuple[dict[str, Any], str, str]:
    """One reported memory: its manifest entry, its path and its file body.

    The four values the distiller cross-checks between entry and frontmatter
    (``memory_id``, ``revision_hash``, ``source_locator``, ``report_fingerprint``)
    are computed once here and written to both, so the pair cannot drift.
    """
    category = str(record["category_hint"])
    source_frontmatter = {"title": str(record["title"]), "tags": list(record["tags"])}
    content = render_frontmatter(source_frontmatter, str(record["body"]))
    rev = revision_hash(content)
    fingerprint = _report_fingerprint(leaf, record, rev, category)
    stem = _slug(Path(str(record["source_locator"])).stem)
    relative_path = f"memories/{category}/{stem}-{rev}.md"
    reported_at = str(leaf["reported_at"])
    entry = {
        "status": "active",
        "relative_path": relative_path,
        "source_locator": str(record["source_locator"]),
        "revision_hash": rev,
        "category_hint": category,
        "redaction_pattern_version": str(leaf["redaction_pattern_version"]),
        "adapter_config_revision": str(leaf["adapter_config_revision"]),
        "report_fingerprint": fingerprint,
        "first_seen_at": reported_at,
        "last_seen_at": reported_at,
        "reported_at": reported_at,
    }
    frontmatter = {
        "schema_version": "memory-net/reported-memory/v1",
        "memory_id": str(record["memory_id"]),
        "source_locator": str(record["source_locator"]),
        "author_node": str(leaf["node_id"]),
        "author_adapter": str(leaf["adapter"]),
        "category_hint": category,
        "revision_hash": rev,
        "report_fingerprint": fingerprint,
        "redaction_pattern_version": str(leaf["redaction_pattern_version"]),
        "adapter_config_revision": str(leaf["adapter_config_revision"]),
        "created_at": reported_at,
        "updated_at": reported_at,
        "reported_at": reported_at,
        "scope": "shared",
        "confidence": "medium",
        "source_type": "agent_memory",
        "redaction_applied": [],
        "source_frontmatter": source_frontmatter,
    }
    return entry, relative_path, render_frontmatter(frontmatter, str(record["body"]))


def _report_fingerprint(
    leaf: dict[str, Any], record: dict[str, Any], rev: str, category: str
) -> str:
    """A clock-free fingerprint over the same shape the reporter uses."""
    payload = stable_json(
        {
            "revision_hash": rev,
            "source_locator": str(record["source_locator"]),
            "category": category,
            "redaction_pattern_version": str(leaf["redaction_pattern_version"]),
            "adapter_config_revision": str(leaf["adapter_config_revision"]),
            "frontmatter": {
                "title": str(record["title"]),
                "tags": list(record["tags"]),
            },
        }
    )
    return f"rpt-{short_hash(payload, 12)}"


def _leaf_health(leaf: dict[str, Any]) -> dict[str, Any]:
    reported_at = str(leaf["reported_at"])
    return {
        "schema_version": "memory-net/health/v1",
        "node_id": str(leaf["node_id"]),
        "role": "leaf",
        "status": "healthy",
        "last_heartbeat_at": reported_at,
        "published_at": reported_at,
        "last_report_at": reported_at,
        "recent_errors": [],
        "redaction_incidents": {"blocked_reports": 0, "scanner_hits": {}},
    }


def _slug(value: str) -> str:
    kept = [char if char.isalnum() else "-" for char in value.strip().lower()]
    return "".join(kept).strip("-").replace("--", "-") or "common"


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    # Explicit, not inherited from the umask: the mode bits are part of the tree
    # a commit sha covers.
    os.chmod(path, 0o644)


def _git(cwd: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update(FIXTURE_IDENTITY)
    # The machine's git config must not reach a fixture whose shas are asserted
    # as constants: a global core.autocrlf or commit.gpgsign would change them.
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        env=env,
    ).stdout


def _show(work: Path, sha: str, relative: str) -> str:
    return _git(work, "show", f"{sha}:{relative}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--remote", type=Path, default=None)
    args = parser.parse_args(argv)
    result = build(args.out, remote=args.remote)
    print(
        json.dumps(
            {
                "work": str(result.work),
                "remote": str(result.remote),
                "master_sha": result.master_sha,
                "leaf_shas": result.leaf_shas,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
