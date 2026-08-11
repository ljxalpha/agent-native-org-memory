"""Operational identity collection for leaf health snapshots.

This module is the **only** place in ``memory_net`` allowed to read the
hostname. Keeping the read on a single call site is what makes it auditable
where local machine identity flows (see mvp_tech_design.md § 8.7.2); an AST
test in ``tests/unit/test_leaf_identity.py`` enforces the invariant.

Collection is deliberately separated from :func:`memory_net.leaf.health.build_health`,
which stays a pure function over its arguments.
"""

from __future__ import annotations

import platform
import re
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

import memory_net
from memory_net.leaf.health import OPS_IDENTITY_FIELDS

#: A ``package_ref`` is only usable as a build id when it pins an exact commit,
#: which ``scripts/install_leaf.sh`` enforces for every ``git+*`` ref.
_COMMIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class OpsIdentityError(RuntimeError):
    """Raised when a required operational identity source is unavailable."""


def read_host() -> str:
    """Return the hostname. The sole ``platform.node()`` call site in the package."""
    return platform.node()


def collect_ops_identity(
    *,
    state_dir: Path,
    adapter_path: Path,
    local_yaml_path: Path,
    generation_dir: Path,
    started_at: str,
) -> dict[str, str]:
    """Collect the operational identity of this leaf installation.

    :param state_dir: ``<MEMORY_NET_HOME>/state``; holds ``install_id``.
    :param adapter_path: adapter YAML whose ``name`` key names the adapter.
    :param local_yaml_path: ``<MEMORY_NET_HOME>/local.yaml``; holds ``package_ref``.
    :param generation_dir: active config generation; may hold the
        ``config_source_master_sha`` marker.
    :param started_at: process start timestamp, injected by the caller so that
        this function never reads the clock and health snapshots stay replayable.
    :returns: a mapping suitable for splatting into ``build_health()``, plus the
        ``build_id_source`` audit key.
    :raises OpsIdentityError: when a required source is missing or empty.
    """
    build_id, build_id_source = _build_id(local_yaml_path)
    identity: dict[str, str] = {
        "install_id": _install_id(state_dir),
        "code_version": memory_net.__version__,
        "build_id": build_id,
        "build_id_source": build_id_source,
        "host": _require("host", read_host()),
        "adapter": _adapter_name(adapter_path),
        "started_at": _require("started_at", started_at),
    }
    master_sha = _optional_marker(generation_dir / "config_source_master_sha")
    if master_sha is not None:
        identity["config_source_master_sha"] = master_sha
    return identity


def split_ops_identity(
    ops_identity: dict[str, str] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split collected identity into ``build_health()`` kwargs and ``extra``.

    The seven schema fields of § 8.7.1 become named ``build_health()``
    arguments; everything else (currently ``build_id_source``) is a diagnostic
    annotation that rides along in ``extra`` rather than entering the validated
    health schema.

    :returns: ``(kwargs, extra)``, both empty when ``ops_identity`` is ``None``.
    """
    if not ops_identity:
        return {}, {}
    kwargs: dict[str, Any] = {
        key: value for key, value in ops_identity.items() if key in OPS_IDENTITY_FIELDS
    }
    extra: dict[str, Any] = {
        key: value
        for key, value in ops_identity.items()
        if key not in OPS_IDENTITY_FIELDS
    }
    return kwargs, extra


def _require(field: str, value: str) -> str:
    if not value.strip():
        raise OpsIdentityError(f"{field} is empty")
    return value.strip()


def _install_id(state_dir: Path) -> str:
    path = state_dir / "install_id"
    if not path.is_file():
        raise OpsIdentityError(f"install_id is missing: {path.name}")
    return _require("install_id", path.read_text(encoding="utf-8"))


def _adapter_name(adapter_path: Path) -> str:
    if not adapter_path.is_file():
        raise OpsIdentityError(f"adapter config is missing: {adapter_path.name}")
    document = _load_yaml_mapping(adapter_path)
    name = document.get("name")
    if not isinstance(name, str) or not name.strip():
        raise OpsIdentityError(f"adapter config has no name: {adapter_path.name}")
    return name.strip()


def build_id_from_package_ref(package_ref: object) -> tuple[str, str]:
    """Return ``(build_id, build_id_source)`` for one ``package_ref``.

    Falls back to the package version when the ref does not pin a commit (the
    development install path, and any local-path ref). The fallback is reported
    through ``build_id_source`` rather than applied silently, so a degraded
    build id is auditable.

    Public because the installer records the same pair in ``build_info.json``:
    the install-time and runtime answers have to be derived identically or the
    audit trail contradicts itself.
    """
    if isinstance(package_ref, str) and "@" in package_ref:
        candidate = package_ref.rsplit("@", 1)[1]
        if _COMMIT_SHA_RE.match(candidate):
            return candidate.lower(), "package_ref"
    return memory_net.__version__, "code_version"


def _build_id(local_yaml_path: Path) -> tuple[str, str]:
    """Return ``(build_id, build_id_source)`` from the installed ``local.yaml``."""
    if local_yaml_path.is_file():
        return build_id_from_package_ref(
            _load_yaml_mapping(local_yaml_path).get("package_ref")
        )
    return memory_net.__version__, "code_version"


def _optional_marker(path: Path) -> str | None:
    """Read a marker file, treating both absence and blankness as "not set"."""
    if not path.is_file():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise OpsIdentityError(f"invalid YAML: {path.name}") from exc
    return document if isinstance(document, dict) else {}
