#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    return validate(root)


def validate(root: Path) -> int:
    config = _load_yaml(root / "config" / "distill.yaml")
    review_gate = _mapping(config.get("review_gate"))
    write_protection = _mapping(config.get("platform_write_protection"))
    has_review_gate = bool(review_gate.get("enabled"))
    has_platform_protection = bool(
        write_protection.get("enabled") or write_protection.get("required")
    )
    if not has_review_gate and not has_platform_protection:
        print(
            "[ABORT] shared pilot requires review_gate.enabled=true or platform_write_protection.enabled=true",
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
