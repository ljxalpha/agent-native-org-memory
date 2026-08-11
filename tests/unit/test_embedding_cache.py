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

from memory_net.distiller.cli import _embedding_cache
from memory_net.distiller.clustering.cache import EmbeddingCache
from memory_net.distiller.clustering.embedder import EmbeddingResult
from memory_net.distiller.clustering.policy import EmbeddingPolicy
from memory_net.distiller.llm.base import LLMProvider, LLMRequest, LLMResponse
from memory_net.distiller.pipeline import DistillationPipeline

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


def test_the_pipeline_recomputes_when_no_cache_is_configured(tmp_path: Path) -> None:
    pipeline = _wired_pipeline(tmp_path, cache=None)

    pipeline.run([_record(1)], run_id="run-a")
    pipeline.run([_record(1)], run_id="run-b")

    embedder = pipeline.embedder
    assert embedder.batches == [["note 1"], ["note 1"]]


def test_the_pipeline_reuses_a_cached_vector_across_runs(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    pipeline = _wired_pipeline(tmp_path, cache=cache)

    pipeline.run([_record(1)], run_id="run-a")
    pipeline.run([_record(1)], run_id="run-b")

    embedder = pipeline.embedder
    assert embedder.batches == [["note 1"]]


def test_the_cli_builds_no_cache_unless_the_profile_enables_one(
    tmp_path: Path,
) -> None:
    policy = EmbeddingPolicy.of("test-cheap", "model-a")

    assert _embedding_cache({}, repo_path=tmp_path, policy=policy) is None
    assert (
        _embedding_cache(
            {"clustering": {"embedding_cache": {"enabled": False}}},
            repo_path=tmp_path,
            policy=policy,
        )
        is None
    )


def test_the_cli_keys_the_cache_on_the_runs_own_policy(tmp_path: Path) -> None:
    policy = EmbeddingPolicy.of("test-cheap", "model-a")

    cache = _embedding_cache(
        {"clustering": {"embedding_cache": {"enabled": True}}},
        repo_path=tmp_path,
        policy=policy,
    )

    assert cache is not None
    assert (cache.backend, cache.model_name) == ("test-cheap", "model-a")
    assert cache.root == tmp_path / ".memory-net" / "embedding_cache"


def test_the_cli_refuses_a_cache_path_outside_the_repo(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="must stay inside the repo"):
        _embedding_cache(
            {"clustering": {"embedding_cache": {"enabled": True, "path": "../cache"}}},
            repo_path=tmp_path,
            policy=EmbeddingPolicy.of("test-cheap", "model-a"),
        )


class CountingEmbedder:
    """Records every batch it is asked to embed."""

    def __init__(self) -> None:
        self.policy = EmbeddingPolicy.of("test-cheap", "model-a")
        self.batches: list[list[str]] = []

    def embed(self, texts: list[str]) -> Any:
        self.batches.append(list(texts))
        return EmbeddingResult(vectors=[[1.0, 2.0, 3.0] for _ in texts])


class OneProductProvider(LLMProvider):
    def generate(self, request: LLMRequest) -> LLMResponse:
        records = list(request.metadata["records"])
        payload = {
            "products": [
                {
                    "title": "Topic",
                    "topic_key": "topic",
                    "category": "common_knowledge",
                    "summary": "Summary.",
                    "evidence": "Evidence.",
                    "citations": [
                        {
                            "memory_id": str(record["memory_id"]),
                            "revision_hash": str(record["revision_hash"]),
                        }
                        for record in records
                    ],
                }
            ]
        }
        return LLMResponse(
            text=json.dumps(payload),
            model="test",
            usage={"input_tokens": 1, "output_tokens": 1},
        )


def _record(index: int) -> dict[str, Any]:
    return {
        "memory_id": f"leaf-a-mem-{index}",
        "revision_hash": f"rev{index:05d}",
        "author_node": "alice",
        "source_branch_sha": "deadbeef",
        "relative_path": f"memories/note-{index}.md",
        "content": f"note {index}",
    }


def _wired_pipeline(tmp_path: Path, *, cache: EmbeddingCache | None) -> Any:
    system = tmp_path / "system.md"
    user = tmp_path / "user.md"
    system.write_text("system", encoding="utf-8")
    user.write_text("records {{ records_json }}", encoding="utf-8")
    return DistillationPipeline(
        provider=OneProductProvider(),
        system_prompt_path=system,
        user_prompt_path=user,
        embedder=CountingEmbedder(),  # type: ignore[arg-type]
        embedding_cache=cache,
    )
