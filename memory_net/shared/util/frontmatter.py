from __future__ import annotations

from typing import Any

import yaml  # type: ignore[import-untyped]


def split_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    if not content.startswith("---\n"):
        return {}, content

    end = content.find("\n---\n", 4)
    if end == -1:
        return {}, content

    raw = content[4:end]
    body = content[end + 5 :]
    loaded = yaml.safe_load(raw) if raw.strip() else {}
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ValueError("frontmatter must be a mapping")
    return loaded, body


def render_frontmatter(frontmatter: dict[str, Any], body: str) -> str:
    if not frontmatter:
        return body
    raw = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{raw}\n---\n{body.lstrip()}"


def body_without_frontmatter(content: str) -> str:
    return split_frontmatter(content)[1]
