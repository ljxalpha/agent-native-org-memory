#!/usr/bin/env python3
"""Check that the shared-pilot write protection is configured coherently.

What this can and cannot establish is the whole point of the file.

It **can** establish that the repository's own configuration is self-consistent:
that a review gate is asked for, that a non-empty CODEOWNERS exists, and that
``config/platform.yaml`` names the same protected branch the distiller pushes
its release branches at.

It **cannot** establish that the platform actually enforces any of it. Branch
protection lives in the hosting platform's settings, behind an organisation
administrator's credentials. Without a token this script therefore prints, in
plain words, that the platform side was *not* verified -- and still exits 0,
because a configuration check failing to reach a platform is not a
configuration error. What it must never do is let a green run be read as
"protection confirmed" (M2-004).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

PLATFORM_TOKEN_ENV = "AOMT_PLATFORM_TOKEN"
NOT_VERIFIED = (
    f"[NOTE] platform-side protection NOT empirically verified: no "
    f"{PLATFORM_TOKEN_ENV} in the environment, so only the repository's own "
    "configuration was checked"
)

#: Injected by tests, and by any operator running this where a platform probe is
#: available. ``None`` means this build carries no probe at all, which is
#: reported rather than silently treated as a pass.
PlatformProbe = Callable[[str, str], list[str]]

_TOKEN_NO_PROBE = (
    f"[NOTE] {PLATFORM_TOKEN_ENV} is set but this build carries no platform "
    "probe; the platform side was still NOT empirically verified"
)


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    return validate(root)


def validate(
    root: Path,
    *,
    environ: Mapping[str, str] | None = None,
    probe: PlatformProbe | None = None,
) -> int:
    environ = os.environ if environ is None else environ
    config = _load_yaml(root / "config" / "distill.yaml")
    review_gate = _mapping(config.get("review_gate"))
    write_protection = _mapping(config.get("platform_write_protection"))
    has_review_gate = bool(review_gate.get("enabled"))
    has_platform_protection = bool(
        write_protection.get("enabled") or write_protection.get("required")
    )
    if not has_review_gate and not has_platform_protection:
        print(
            "[ABORT] shared pilot requires review_gate.enabled=true or "
            "platform_write_protection.enabled=true",
            file=sys.stderr,
        )
        return 3
    if has_review_gate:
        codeowners = root / str(
            review_gate.get("codeowners_path") or ".github/CODEOWNERS"
        )
        if (
            not codeowners.exists()
            or not codeowners.read_text(encoding="utf-8").strip()
        ):
            print(
                f"[ABORT] review gate requires non-empty CODEOWNERS: {codeowners}",
                file=sys.stderr,
            )
            return 4
        code = _check_branch_protection(root, review_gate, environ, probe)
        if code:
            return code
        return 0
    # Protection is claimed by configuration alone, with no review gate to fall
    # back on. That is exactly the case where a bare exit 0 reads as "the
    # platform enforces this", so the limitation is stated here too.
    print("[OK] platform_write_protection is declared in config/distill.yaml")
    print(NOT_VERIFIED if not environ.get(PLATFORM_TOKEN_ENV) else _TOKEN_NO_PROBE)
    return 0


def _check_branch_protection(
    root: Path,
    review_gate: dict[str, Any],
    environ: Mapping[str, str],
    probe: PlatformProbe | None,
) -> int:
    """The configuration half of the guarantee, then an honest verdict."""
    platform = _load_yaml(root / "config" / "platform.yaml")
    protection = _mapping(platform.get("branch_protection"))
    writers = protection.get("master_writers_only")
    if not isinstance(writers, list) or not [
        item for item in writers if isinstance(item, str) and item.strip()
    ]:
        print(
            "[ABORT] review gate requires a non-empty "
            "platform.yaml branch_protection.master_writers_only",
            file=sys.stderr,
        )
        return 5
    target = str(review_gate.get("pr_target_branch") or "master")
    protected = str(protection.get("protected_branch") or "")
    if protected != target:
        print(
            "[ABORT] branch_protection.protected_branch "
            f"({protected or 'unset'}) must match review_gate.pr_target_branch "
            f"({target})",
            file=sys.stderr,
        )
        return 6
    print(f"[OK] review gate targets {target}, writable by {', '.join(writers)}")
    token = environ.get(PLATFORM_TOKEN_ENV)
    if not token:
        # Deliberately exit 0 with a stated limitation rather than pretending:
        # the alternative is a green check that reads as evidence of something
        # this run never looked at.
        print(NOT_VERIFIED)
        return 0
    if probe is None:
        print(_TOKEN_NO_PROBE)
        return 0
    findings = probe(target, token)
    for finding in findings:
        print(f"[ABORT] platform probe: {finding}", file=sys.stderr)
    if findings:
        return 7
    print(f"[OK] platform probe confirms protection on {target}")
    return 0


def _load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise SystemExit(f"config must be a mapping: {path}")
    return value


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


if __name__ == "__main__":
    raise SystemExit(main())
