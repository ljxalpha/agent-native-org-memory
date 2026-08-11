"""Fail-closed probes run before an install commits to anything.

The installer's expensive, hard-to-undo steps -- building a venv, cloning the
team repo, pushing a leaf branch, registering cron -- all happen before anything
would notice that the Python is too old, the state directory is on a network
share, or the remote's schemas are unreadable. Every one of those produces a
half-installed leaf whose eventual failure looks unrelated to its cause.

These probes move the verdict to the front. Each returns a
:class:`ProbeFailure` naming the stage that failed, and the CLI entry point
turns that into a non-zero exit with the stage on stderr, so the operator learns
*which* precondition broke rather than reading a traceback from four steps
later.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import memory_net
from memory_net.leaf.identity import build_id_from_package_ref
from memory_net.shared.schema import (
    SchemaError,
    validate_bootstrap_state,
    validate_master_manifest,
    validate_team_product_frontmatter,
)
from memory_net.shared.util.filesystem import local_filesystem_refusal
from memory_net.shared.util.frontmatter import split_frontmatter

#: Lower bound from ``pyproject.toml``'s ``requires-python``. Kept as a tuple
#: rather than re-parsing the metadata so the probe works from a source
#: checkout, where the installed distribution may not exist yet.
MINIMUM_PYTHON = (3, 11)
#: ``git switch --orphan`` and ``git restore`` are both used by the install and
#: leaf paths; 2.23 is the release that introduced them.
MINIMUM_GIT = (2, 23)
#: A venv plus a repo clone plus one config generation. Well under any real
#: install, but enough to catch a directory that is already full.
MINIMUM_FREE_BYTES = 512 * 1024 * 1024

PREFLIGHT_STAGE = "PREFLIGHT"
SCHEMA_PROBE_STAGE = "SCHEMA_PROBE"


@dataclass(frozen=True)
class ProbeFailure:
    """One failed precondition, named by the install stage it belongs to."""

    stage: str
    reason: str

    def __str__(self) -> str:
        return f"[{self.stage}] {self.reason}"


def preflight_probe(
    memory_net_home: Path,
    *,
    python_version: tuple[int, ...] | None = None,
    git_version: tuple[int, ...] | None = None,
    free_bytes: int | None = None,
) -> ProbeFailure | None:
    """Check the interpreter, git, and the target directory.

    Every input is injectable so the failure branches are testable without an
    unusual machine; ``None`` means "measure the real thing".

    :returns: the first failure, or ``None`` when every precondition holds.
    """
    python_version = python_version or sys.version_info[:2]
    if python_version < MINIMUM_PYTHON:
        return ProbeFailure(
            PREFLIGHT_STAGE,
            f"python {_render(python_version)} is older than the required "
            f"{_render(MINIMUM_PYTHON)}",
        )

    resolved_git = git_version if git_version is not None else _git_version()
    if resolved_git is None:
        return ProbeFailure(PREFLIGHT_STAGE, "git is not available")
    if resolved_git < MINIMUM_GIT:
        return ProbeFailure(
            PREFLIGHT_STAGE,
            f"git {_render(resolved_git)} is older than the required "
            f"{_render(MINIMUM_GIT)}",
        )

    refusal = local_filesystem_refusal(memory_net_home, label="MEMORY_NET_HOME")
    if refusal is not None:
        return ProbeFailure(PREFLIGHT_STAGE, refusal)

    writable = _writable_refusal(memory_net_home)
    if writable is not None:
        return ProbeFailure(PREFLIGHT_STAGE, writable)

    resolved_free = (
        free_bytes if free_bytes is not None else _free_bytes(memory_net_home)
    )
    if resolved_free < MINIMUM_FREE_BYTES:
        return ProbeFailure(
            PREFLIGHT_STAGE,
            f"MEMORY_NET_HOME has {resolved_free} bytes free, "
            f"below the required {MINIMUM_FREE_BYTES}",
        )
    return None


def schema_probe(
    *,
    bootstrap_state: str,
    manifest: str,
    team_products: Iterable[str] = (),
    build_version: str | None = None,
    build_api: int = 1,
    build_id: str | None = None,
) -> ProbeFailure | None:
    """Validate the remote's schemas before the install adopts them.

    The installer previously only checked that these files *existed*. A remote
    whose ``bootstrap_state.json`` declares an incompatible code range, or whose
    products carry an unreadable frontmatter, would install cleanly and fail
    later during the first sync, far from the cause.

    :param team_products: raw text of representative ``team/`` products; one is
        enough to catch a schema-wide break, and reading all of them would make
        install time scale with the size of the team's memory.
    """
    try:
        state = _parse_json_mapping(bootstrap_state, "bootstrap_state.json")
        validate_bootstrap_state(
            state,
            build_version=build_version or memory_net.__version__,
            build_api=build_api,
            build_id=build_id,
        )
    except SchemaError as exc:
        return ProbeFailure(SCHEMA_PROBE_STAGE, f"bootstrap_state.json: {exc}")

    try:
        validate_master_manifest(
            _parse_json_mapping(manifest, "manifest.json"),
            build_version=build_version or memory_net.__version__,
        )
    except SchemaError as exc:
        return ProbeFailure(SCHEMA_PROBE_STAGE, f"manifest.json: {exc}")

    for index, product in enumerate(team_products):
        try:
            frontmatter, _body = split_frontmatter(product)
        except Exception as exc:  # noqa: BLE001 - any parse failure is a refusal
            return ProbeFailure(
                SCHEMA_PROBE_STAGE,
                f"team product {index}: unreadable frontmatter: {exc}",
            )
        try:
            validate_team_product_frontmatter(frontmatter)
        except SchemaError as exc:
            return ProbeFailure(SCHEMA_PROBE_STAGE, f"team product {index}: {exc}")
    return None


def write_build_info(
    state_dir: Path,
    *,
    package_ref: str,
    master_sha: str,
    installed_at: str,
) -> dict[str, str]:
    """Record what this install actually installed.

    Without it there is no on-disk answer to "which build is this leaf running,
    and against which master snapshot was it installed" that survives the
    install script exiting -- ``local.yaml`` holds the ref but not the resolved
    build id, nor the degradation that produced it.

    :returns: the recorded mapping, so callers can assert on it directly.
    """
    build_id, build_id_source = build_id_from_package_ref(package_ref)
    build_info = {
        "package_ref": package_ref,
        "build_id": build_id,
        "build_id_source": build_id_source,
        "code_version": memory_net.__version__,
        "installed_at": installed_at,
        "master_sha": master_sha,
    }
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / "build_info.json"
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_text(
        json.dumps(build_info, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    tmp.replace(target)
    return build_info


def _parse_json_mapping(raw: str, label: str) -> dict[str, Any]:
    """Parse JSON, reporting a syntax error as a schema failure.

    Strictly JSON, matching how the runtime reads these files. A YAML parser
    would accept a superset -- unquoted keys, comments, bare scalars -- so a
    remote the runtime cannot read would sail through the probe and fail later
    during a sync, which is the exact class of failure the probe exists to
    prevent.
    """
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"{label} is not parseable: {exc}") from exc
    if not isinstance(document, dict):
        raise SchemaError(f"{label} is not a mapping")
    return document


def _probe_build_id(package_ref: str | None) -> str | None:
    """Resolve a package ref to a build id, or ``None`` when it pins no commit.

    Two refusals, both deliberate:

    * A ref that pins no commit yields ``None`` rather than the code-version
      fallback. ``approved_build_ids`` is a commit allowlist; a version string
      in it either never matches or matches by accident.
    * Only a ``git+`` ref can yield a build id at all. A local path may also end
      in ``@<40 hex>`` -- the installer only enforces the sha rule for ``git+``
      refs -- so accepting one would let a mutable directory named after an
      approved commit satisfy the allowlist.
    """
    if package_ref is None or not package_ref.startswith("git+"):
        return None
    build_id, source = build_id_from_package_ref(package_ref)
    return build_id if source == "package_ref" else None


def _render(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


def _git_version() -> tuple[int, ...] | None:
    if shutil.which("git") is None:
        return None
    result = subprocess.run(
        ["git", "--version"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    for token in result.stdout.split():
        parts = token.split(".")
        if parts[0].isdigit():
            return tuple(int(part) for part in parts if part.isdigit())
    return None


def _writable_refusal(memory_net_home: Path) -> str | None:
    """Confirm the install can actually create files under its home.

    The probe file gets a unique name and is created exclusively, so it can
    never truncate something that was already there, follow a planted symlink,
    or collide with a concurrent probe. Cleanup runs in ``finally`` so a failure
    after creation still leaves nothing behind.
    """
    probe: Path | None = None
    try:
        memory_net_home.mkdir(parents=True, exist_ok=True)
        probe = memory_net_home / f".write-probe.{uuid.uuid4().hex}"
        with probe.open("x", encoding="utf-8"):
            pass
    except OSError as exc:
        return f"MEMORY_NET_HOME is not writable: {memory_net_home}: {exc}"
    finally:
        if probe is not None:
            probe.unlink(missing_ok=True)
    return None


def _free_bytes(memory_net_home: Path) -> int:
    return shutil.disk_usage(memory_net_home).free


def main(argv: list[str] | None = None) -> int:
    """CLI shim the install script calls; prints the stage and exits non-zero."""
    parser = argparse.ArgumentParser(prog="memory-net-install-probe")
    sub = parser.add_subparsers(dest="stage", required=True)

    preflight = sub.add_parser("preflight")
    preflight.add_argument("--home", required=True, type=Path)

    schema = sub.add_parser("schema")
    schema.add_argument("--bootstrap-state", required=True, type=Path)
    schema.add_argument("--manifest", required=True, type=Path)
    schema.add_argument("--team-product", type=Path, action="append", default=[])
    schema.add_argument(
        "--package-ref",
        help=(
            "resolved into the build id fed to approved_build_ids; a ref that "
            "does not pin a commit yields no build id rather than a fake one"
        ),
    )

    build_info = sub.add_parser("build-info")
    build_info.add_argument("--state-dir", required=True, type=Path)
    build_info.add_argument("--package-ref", required=True)
    build_info.add_argument("--master-sha", required=True)
    build_info.add_argument("--installed-at", required=True)

    args = parser.parse_args(argv)
    if args.stage == "build-info":
        write_build_info(
            args.state_dir,
            package_ref=args.package_ref,
            master_sha=args.master_sha,
            installed_at=args.installed_at,
        )
        return 0
    if args.stage == "preflight":
        failure = preflight_probe(args.home)
    else:
        failure = schema_probe(
            bootstrap_state=args.bootstrap_state.read_text(encoding="utf-8"),
            manifest=args.manifest.read_text(encoding="utf-8"),
            team_products=[
                path.read_text(encoding="utf-8") for path in args.team_product
            ],
            build_id=_probe_build_id(args.package_ref),
        )
    if failure is not None:
        print(str(failure), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
