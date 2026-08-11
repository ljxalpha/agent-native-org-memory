from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Operational identity keys written by :func:`build_health` when supplied.
#: Order is the documented order in mvp_tech_design.md § 8.7.1.
OPS_IDENTITY_FIELDS: tuple[str, ...] = (
    "adapter",
    "install_id",
    "code_version",
    "host",
    "build_id",
    "config_source_master_sha",
    "started_at",
)


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _checked_ops_field(name: str, value: str | None) -> str | None:
    """Validate an optional operational identity value.

    Returns ``None`` (meaning "do not write this key") when the caller omitted
    the field. A supplied value must be a non-empty ``str``.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    if not value:
        raise ValueError(f"{name} must not be empty")
    return value


def build_health(
    *,
    node_id: str,
    role: str = "leaf",
    status: str = "healthy",
    config_revision: str | None = None,
    last_report_at: str | None = None,
    last_pull_at: str | None = None,
    redaction_pattern_version: str | None = None,
    recent_errors: list[str] | None = None,
    redaction_incidents: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    published: bool = True,
    published_at: str | None = None,
    adapter: str | None = None,
    install_id: str | None = None,
    code_version: str | None = None,
    host: str | None = None,
    build_id: str | None = None,
    config_source_master_sha: str | None = None,
    started_at: str | None = None,
) -> dict[str, Any]:
    """Build a ``.health.json`` payload.

    This is a pure function over its arguments: it never reads the hostname,
    the environment or the filesystem. Operational identity collection is the
    responsibility of :mod:`memory_net.leaf.identity`.

    The seven operational identity parameters (see mvp_tech_design.md § 8.7.1)
    are optional; an omitted one is not written at all, so callers that pass
    none of them get exactly the pre-existing payload.
    """
    ops_identity = {
        name: _checked_ops_field(name, value)
        for name, value in (
            ("adapter", adapter),
            ("install_id", install_id),
            ("code_version", code_version),
            ("host", host),
            ("build_id", build_id),
            ("config_source_master_sha", config_source_master_sha),
            ("started_at", started_at),
        )
    }
    now = utc_now()
    incidents: dict[str, Any] = {
        "blocked_reports": 0,
        "scanner_hits": {},
    }
    if redaction_incidents:
        if redaction_incidents.get("blocked_reports") is not None:
            incidents["blocked_reports"] = int(redaction_incidents["blocked_reports"])
        scanner_hits = redaction_incidents.get("scanner_hits")
        if isinstance(scanner_hits, dict):
            merged = dict(incidents["scanner_hits"])
            merged.update({str(key): int(value) for key, value in scanner_hits.items()})
            incidents["scanner_hits"] = merged
    health: dict[str, Any] = {
        "schema_version": "memory-net/health/v1",
        "node_id": node_id,
        "role": role,
        "status": status,
        "last_heartbeat_at": now,
        "recent_errors": recent_errors or [],
        "redaction_incidents": incidents,
    }
    if published:
        health["published_at"] = published_at or now
    if config_revision:
        health["config_revision"] = config_revision
    if last_report_at:
        health["last_report_at"] = last_report_at
    if last_pull_at:
        health["last_pull_at"] = last_pull_at
    if redaction_pattern_version is not None:
        health["redaction_pattern_version"] = redaction_pattern_version
    for name in OPS_IDENTITY_FIELDS:
        value = ops_identity[name]
        if value is not None:
            health[name] = value
    if extra:
        health.update(extra)
    return health


def write_health(path: Path, health: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(health, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    tmp.replace(path)
