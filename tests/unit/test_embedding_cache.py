"""The embedding cache: what it stores, and what it must never store.

The plaintext assertion is the point of the file. A cache entry that carried the
record text would be leaf memory content written to disk outside the run's
transaction and outside the output scanner's write-time gate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from memory_net.distiller.clustering.cache import EmbeddingCache

SECRET = "the incident postmortem named the on-call engineer"


def _cache(tmp_path: Path, *, backend: str = "test-cheap", model: str = "m") -> Any:
    return EmbeddingCache(
        tmp_path / "embedding_cache", backend=backend, model_name=model
    )


def test_a_miss_computes_and_a_hit_does_not(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    calls: list[list[str]] = []

    def compute(texts: list[str]) -> list[list[float]]:
        calls.append(texts)
        return [[1.0, 2.0, 3.0] for _ in texts]

    first = cache.resolve([SECRET], compute)
    second = cache.resolve([SECRET], compute)

    assert first == second == [[1.0, 2.0, 3.0]]
    assert calls == [[SECRET]]


def test_only_the_missing_texts_are_computed(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.put("a", [1.0])
    calls: list[list[str]] = []

    def compute(texts: list[str]) -> list[list[float]]:
        calls.append(texts)
        return [[2.0] for _ in texts]

    vectors = cache.resolve(["a", "b", "c"], compute)

    assert calls == [["b", "c"]]
    assert vectors == [[1.0], [2.0], [2.0]]


def test_another_backend_or_model_misses(tmp_path: Path) -> None:
    _cache(tmp_path).put(SECRET, [1.0])

    assert _cache(tmp_path, backend="fastembed").get(SECRET) is None
    assert _cache(tmp_path, model="other").get(SECRET) is None
    assert _cache(tmp_path).get(SECRET) == [1.0]


def test_a_corrupt_entry_is_a_miss_not_a_crash(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.put(SECRET, [1.0])
    entry = next((tmp_path / "embedding_cache").rglob("*.json"))
    entry.write_text("{ truncated", encoding="utf-8")

    assert cache.get(SECRET) is None

    recomputed = cache.resolve([SECRET], lambda texts: [[9.0] for _ in texts])
    assert recomputed == [[9.0]]


@pytest.mark.parametrize(
    "payload",
    ['{"vector": "1.0"}', '{"vector": []}', '{"vector": [true]}', "[]"],
)
def test_an_entry_of_the_wrong_shape_is_a_miss(tmp_path: Path, payload: str) -> None:
    cache = _cache(tmp_path)
    cache.put(SECRET, [1.0])
    entry = next((tmp_path / "embedding_cache").rglob("*.json"))
    entry.write_text(payload, encoding="utf-8")

    assert cache.get(SECRET) is None


def test_the_cache_file_holds_a_vector_and_nothing_else(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.put(SECRET, [1.0, 2.0])
    entry = next((tmp_path / "embedding_cache").rglob("*.json"))
    text = entry.read_text(encoding="utf-8")

    assert json.loads(text) == {"vector": [1.0, 2.0]}
    # No plaintext, and not even a word of it: the key is a digest of a digest.
    assert SECRET not in text
    for word in SECRET.split():
        assert word not in text
    assert SECRET not in entry.as_posix()


def test_the_key_binds_the_vector_to_its_policy(tmp_path: Path) -> None:
    cheap = _cache(tmp_path)
    real = _cache(tmp_path, backend="fastembed")

    assert cheap.key(SECRET) != real.key(SECRET)
    assert cheap.key(SECRET) == cheap.key(SECRET)
    assert SECRET not in cheap.key(SECRET)
