from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from memory_net.shared.util.frontmatter import body_without_frontmatter


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def short_hash(value: str, length: int = 8) -> str:
    return sha256_text(value)[:length]


def revision_hash(content: str) -> str:
    return short_hash(body_without_frontmatter(content).strip())


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def hash_path_bytes(pairs: list[tuple[Path, bytes]]) -> str:
    h = hashlib.sha256()
    for path, data in sorted(pairs, key=lambda item: item[0].as_posix()):
        h.update(path.as_posix().encode("utf-8"))
        h.update(b"\0")
        h.update(data)
        h.update(b"\0")
    return h.hexdigest()
