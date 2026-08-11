"""Tests for the install-time preflight and schema probes.

Every failure branch is driven through injected values rather than an unusual
machine, so the negative cases actually run in CI instead of being asserted in a
comment.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import memory_net
from memory_net.leaf import install_probe
from memory_net.leaf.install_probe import (
    MINIMUM_FREE_BYTES,
    PREFLIGHT_STAGE,
    SCHEMA_PROBE_STAGE,
    preflight_probe,
    schema_probe,
)

BOOTSTRAP_STATE = {
    "schema_version": 1,
    "compatible_build_api_range": ">=1,<2",
    "compatible_code_range": ">=0.1.0,<0.2.0",
    "manifest_schema_version": 1,
    "record_frontmatter_schema_version": 1,
    "product_frontmatter_schema_version": 1,
    "health_schema_version": 1,
    "owner_schema_version": 1,
}
MASTER_MANIFEST = {
    "schema_version": "memory-net/master-manifest/v1",
    "compatible_leaf_range": ">=0.1.0,<0.2.0",
    "products_active": {},
}
TEAM_PRODUCT = """---
schema_version: memory-net/team-product/v1
distillation_id: d-0001
category: practice
---
A team practice.
"""


def test_preflight_accepts_a_healthy_environment(tmp_path: Path) -> None:
    assert preflight_probe(tmp_path / "home", free_bytes=MINIMUM_FREE_BYTES) is None


def test_preflight_rejects_an_old_python(tmp_path: Path) -> None:
    failure = preflight_probe(
        tmp_path / "home",
        python_version=(3, 10),
        free_bytes=MINIMUM_FREE_BYTES,
    )

    assert failure is not None
    assert failure.stage == PREFLIGHT_STAGE
    assert "python 3.10" in failure.reason


def test_preflight_rejects_an_old_git(tmp_path: Path) -> None:
    failure = preflight_probe(
        tmp_path / "home",
        git_version=(2, 20),
        free_bytes=MINIMUM_FREE_BYTES,
    )

    assert failure is not None
    assert "git 2.20" in failure.reason


def test_preflight_rejects_a_missing_git(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(install_probe, "_git_version", lambda: None)

    failure = preflight_probe(tmp_path / "home", free_bytes=MINIMUM_FREE_BYTES)

    assert failure is not None
    assert "git is not available" in failure.reason


def test_preflight_rejects_a_read_only_home(tmp_path: Path) -> None:
    """State on an unwritable directory produces a half-built install."""
    parent = tmp_path / "locked"
    parent.mkdir()
    parent.chmod(0o500)
    try:
        failure = preflight_probe(parent / "home", free_bytes=MINIMUM_FREE_BYTES)
    finally:
        parent.chmod(0o700)

    assert failure is not None
    assert "not writable" in failure.reason


def test_preflight_rejects_a_non_local_filesystem(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Lock primitives are unreliable on a network share."""
    monkeypatch.setattr(
        install_probe,
        "local_filesystem_refusal",
        lambda _path, *, label: f"{label} nfs",
    )

    failure = preflight_probe(tmp_path / "home", free_bytes=MINIMUM_FREE_BYTES)

    assert failure is not None
    assert "MEMORY_NET_HOME nfs" in failure.reason


def test_the_write_probe_never_touches_an_existing_file(tmp_path: Path) -> None:
    """A fixed probe name would truncate whatever already had it."""
    home = tmp_path / "home"
    home.mkdir()
    existing = home / ".write-probe"
    existing.write_text("precious", encoding="utf-8")

    assert preflight_probe(home, free_bytes=MINIMUM_FREE_BYTES) is None

    assert existing.read_text(encoding="utf-8") == "precious"
    assert [path.name for path in home.glob(".write-probe.*")] == []


def test_preflight_rejects_a_full_disk(tmp_path: Path) -> None:
    failure = preflight_probe(tmp_path / "home", free_bytes=1024)

    assert failure is not None
    assert "bytes free" in failure.reason


def test_schema_probe_accepts_a_healthy_remote() -> None:
    assert (
        schema_probe(
            bootstrap_state=json.dumps(BOOTSTRAP_STATE),
            manifest=json.dumps(MASTER_MANIFEST),
            team_products=[TEAM_PRODUCT],
        )
        is None
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("{not json", "not parseable"),
        # Valid YAML, invalid JSON: the runtime reads these files with a JSON
        # parser, so a YAML-tolerant probe would pass a remote it cannot read.
        (
            "schema_version: 1\ncompatible_code_range: '>=0.1.0,<0.2.0'\n",
            "not parseable",
        ),
        ("# a comment\n{}", "not parseable"),
        ("[1, 2]", "not a mapping"),
        ('{"schema_version": 2}', "unsupported bootstrap schema_version"),
    ],
)
def test_schema_probe_rejects_a_broken_bootstrap_state(raw: str, expected: str) -> None:
    failure = schema_probe(bootstrap_state=raw, manifest=json.dumps(MASTER_MANIFEST))

    assert failure is not None
    assert failure.stage == SCHEMA_PROBE_STAGE
    assert expected in failure.reason


def test_schema_probe_rejects_an_incompatible_code_range() -> None:
    state = {**BOOTSTRAP_STATE, "compatible_code_range": ">=9.0.0,<10.0.0"}

    failure = schema_probe(
        bootstrap_state=json.dumps(state),
        manifest=json.dumps(MASTER_MANIFEST),
    )

    assert failure is not None
    assert "compatible_code_range" in failure.reason


def test_schema_probe_rejects_a_broken_manifest() -> None:
    failure = schema_probe(
        bootstrap_state=json.dumps(BOOTSTRAP_STATE),
        manifest=json.dumps({"schema_version": "memory-net/master-manifest/v9"}),
    )

    assert failure is not None
    assert "manifest.json" in failure.reason


def test_schema_probe_rejects_a_broken_product_frontmatter() -> None:
    broken = TEAM_PRODUCT.replace("memory-net/team-product/v1", "memory-net/wrong/v1")

    failure = schema_probe(
        bootstrap_state=json.dumps(BOOTSTRAP_STATE),
        manifest=json.dumps(MASTER_MANIFEST),
        team_products=[broken],
    )

    assert failure is not None
    assert "team product 0" in failure.reason


def test_schema_probe_uses_the_running_code_version_by_default() -> None:
    """The compatibility check is meaningless if it invents its own version."""
    state = {
        **BOOTSTRAP_STATE,
        "compatible_code_range": f">={memory_net.__version__},<0.2.0",
    }

    assert (
        schema_probe(
            bootstrap_state=json.dumps(state),
            manifest=json.dumps(MASTER_MANIFEST),
        )
        is None
    )


def test_cli_prints_the_stage_and_exits_non_zero(tmp_path: Path, capsys: Any) -> None:
    """The install script reads the exit code; the operator reads the stage."""
    bootstrap = tmp_path / "bootstrap_state.json"
    bootstrap.write_text("{not json", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(MASTER_MANIFEST), encoding="utf-8")

    exit_code = install_probe.main(
        [
            "schema",
            "--bootstrap-state",
            str(bootstrap),
            "--manifest",
            str(manifest),
        ]
    )

    assert exit_code == 1
    assert SCHEMA_PROBE_STAGE in capsys.readouterr().err


def test_cli_preflight_succeeds_quietly(tmp_path: Path, capsys: Any) -> None:
    assert install_probe.main(["preflight", "--home", str(tmp_path / "home")]) == 0
    assert capsys.readouterr().err == ""


SAMPLE_SHA = "0123456789abcdef0123456789abcdef01234567"
GIT_REF = f"git+ssh://git@example.invalid/org/aomt.git@{SAMPLE_SHA}"


def test_schema_probe_rejects_an_incompatible_leaf_range() -> None:
    """compatible_leaf_range existed but the installer never consulted it."""
    manifest = {**MASTER_MANIFEST, "compatible_leaf_range": ">=9.0.0,<10.0.0"}

    failure = schema_probe(
        bootstrap_state=json.dumps(BOOTSTRAP_STATE),
        manifest=json.dumps(manifest),
    )

    assert failure is not None
    assert "compatible_leaf_range" in failure.reason


def test_schema_probe_rejects_a_build_id_outside_the_remote_allowlist() -> None:
    state = {**BOOTSTRAP_STATE, "approved_build_ids": ["f" * 40]}

    failure = schema_probe(
        bootstrap_state=json.dumps(state),
        manifest=json.dumps(MASTER_MANIFEST),
        build_id=SAMPLE_SHA,
    )

    assert failure is not None
    assert "bootstrap_state.json" in failure.reason


def test_schema_probe_accepts_a_build_id_inside_the_remote_allowlist() -> None:
    state = {**BOOTSTRAP_STATE, "approved_build_ids": [SAMPLE_SHA]}

    assert (
        schema_probe(
            bootstrap_state=json.dumps(state),
            manifest=json.dumps(MASTER_MANIFEST),
            build_id=SAMPLE_SHA,
        )
        is None
    )


def test_a_remote_without_an_allowlist_admits_every_build() -> None:
    """Regression: the allowlist is opt-in, so existing installs keep working.

    Adding the key unconditionally would fail-close every leaf already in the
    field, which is why bootstrap_state.json does not ship with it.
    """
    assert "approved_build_ids" not in BOOTSTRAP_STATE

    assert (
        schema_probe(
            bootstrap_state=json.dumps(BOOTSTRAP_STATE),
            manifest=json.dumps(MASTER_MANIFEST),
            build_id=SAMPLE_SHA,
        )
        is None
    )


@pytest.mark.parametrize(
    ("package_ref", "expected"),
    [
        (GIT_REF, SAMPLE_SHA),
        ("/local/path/to/aomt", None),
        # A local path may also end in @<40 hex>; the installer only enforces
        # the sha rule for git+ refs, so a mutable directory named after an
        # approved commit must not satisfy the allowlist.
        (f"/tmp/pkg@{SAMPLE_SHA}", None),
        (f"./checkout@{SAMPLE_SHA}", None),
        (f"git+ssh://git@example.invalid/org/aomt.git@{SAMPLE_SHA[:39]}", None),
        (None, None),
    ],
)
def test_only_a_commit_pinning_ref_yields_a_build_id(
    package_ref: str | None, expected: str | None
) -> None:
    """A version string in a commit allowlist matches by accident or never."""
    assert install_probe._probe_build_id(package_ref) == expected


def test_build_info_records_the_resolved_build_and_its_source(tmp_path: Path) -> None:
    build_info = install_probe.write_build_info(
        tmp_path / "state",
        package_ref=GIT_REF,
        master_sha="a" * 40,
        installed_at="2026-04-20T09:00:00Z",
    )

    written = json.loads(
        (tmp_path / "state" / "build_info.json").read_text(encoding="utf-8")
    )
    assert written == build_info
    assert written == {
        "package_ref": GIT_REF,
        "build_id": SAMPLE_SHA,
        "build_id_source": "package_ref",
        "code_version": memory_net.__version__,
        "installed_at": "2026-04-20T09:00:00Z",
        "master_sha": "a" * 40,
    }


def test_build_info_reports_a_degraded_build_id_rather_than_hiding_it(
    tmp_path: Path,
) -> None:
    build_info = install_probe.write_build_info(
        tmp_path / "state",
        package_ref="/local/path/to/aomt",
        master_sha="a" * 40,
        installed_at="2026-04-20T09:00:00Z",
    )

    assert build_info["build_id"] == memory_net.__version__
    assert build_info["build_id_source"] == "code_version"


def test_build_info_agrees_with_the_runtime_identity(tmp_path: Path) -> None:
    """Install time and runtime must derive the build id the same way.

    Two derivations that disagree turn the audit trail into a contradiction.
    """
    from memory_net.leaf.identity import build_id_from_package_ref

    build_info = install_probe.write_build_info(
        tmp_path / "state",
        package_ref=GIT_REF,
        master_sha="a" * 40,
        installed_at="2026-04-20T09:00:00Z",
    )

    assert build_id_from_package_ref(GIT_REF) == (
        build_info["build_id"],
        build_info["build_id_source"],
    )
