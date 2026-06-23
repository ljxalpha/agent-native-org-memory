from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]


class FastEmbedder:
    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5") -> None:
        self.model_name = model_name

    def embed(self, texts: list[str]) -> EmbeddingResult:
        if os.environ.get("MEMORY_NET_USE_FASTEMBED") != "1":
            return EmbeddingResult(vectors=[_cheap_vector(text) for text in texts])
        try:
            from fastembed import TextEmbedding
        except (ImportError, OSError, RuntimeError):
            return EmbeddingResult(vectors=[_cheap_vector(text) for text in texts])
        model = TextEmbedding(model_name=self.model_name)
        return EmbeddingResult(vectors=[list(vector) for vector in model.embed(texts)])


def _cheap_vector(text: str) -> list[float]:
    words = text.lower().split()
    return [
        float(len(words)),
        float(len(set(words))),
        float(sum(len(word) for word in words)),
    ]
