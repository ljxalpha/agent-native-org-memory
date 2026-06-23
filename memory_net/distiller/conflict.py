from __future__ import annotations

from typing import Any

CONFLICT_MARKER = "⚠️ 存在分歧"


def preserve_both(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def normalize_conflicts(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("conflicts must be a list")
    conflicts: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError("conflict item must be an object")
        positions = item.get("positions") or []
        if not isinstance(positions, list):
            raise ValueError("conflict positions must be a list")
        normalized_positions: list[dict[str, Any]] = []
        for position_index, position in enumerate(positions, start=1):
            if not isinstance(position, dict):
                raise ValueError("conflict position must be an object")
            claim = str(position.get("claim") or "").strip()
            if not claim:
                raise ValueError("conflict position claim is required")
            normalized_positions.append(
                {
                    "position_id": str(
                        position.get("position_id") or f"pos-{index}-{position_index}"
                    ),
                    "claim": claim,
                    "contributors": sorted(
                        {str(value) for value in position.get("contributors") or []}
                    ),
                    "citation_refs": position.get("citation_refs") or [],
                }
            )
        if normalized_positions:
            conflicts.append(
                {
                    "description": str(item.get("description") or "").strip(),
                    "positions": normalized_positions,
                }
            )
    return conflicts
