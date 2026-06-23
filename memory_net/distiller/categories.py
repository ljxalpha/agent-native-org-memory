from __future__ import annotations

import re


def canonical_category(value: str) -> str:
    category = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip().lower())
    category = re.sub(r"_+", "_", category).strip("_")
    return category or "common_knowledge"
