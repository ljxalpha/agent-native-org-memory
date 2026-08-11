from __future__ import annotations

import os
from dataclasses import dataclass

from memory_net.distiller.clustering.policy import (
    FASTEMBED,
    TEST_CHEAP,
    EmbeddingPolicy,
)


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]


class FastEmbedder:
    """Embeds text using whichever backend the run's policy selected.

    The backend is injected rather than read from the environment here, because
    the clusterer needs the same answer and two independent reads can disagree
    without anything at runtime noticing (§5.5.7).
    """

    def __init__(
        self,
        model_name: str | None = None,
        *,
        policy: EmbeddingPolicy | None = None,
    ) -> None:
        self.policy = policy
        # An explicit model_name still wins, so the pre-policy call sites keep
        # working unchanged.
        self.model_name = model_name or (
            policy.model_name if policy is not None else EmbeddingPolicy().model_name
        )

    def embed(self, texts: list[str]) -> EmbeddingResult:
        if self._backend() == TEST_CHEAP:
            return EmbeddingResult(vectors=[_cheap_vector(text) for text in texts])
        try:
            from fastembed import TextEmbedding
        except (ImportError, OSError, RuntimeError) as exc:
            if self.policy is not None:
                # A policy naming fastembed is a decision, not a preference.
                # Falling back would cluster three-dimensional word counts with
                # DBSCAN parameters tuned for semantic distance: meaningless
                # products behind entirely ordinary-looking metrics.
                raise RuntimeError(
                    f"embedding backend {FASTEMBED!r} could not be loaded: "
                    f"{type(exc).__name__}"
                ) from exc
            return EmbeddingResult(vectors=[_cheap_vector(text) for text in texts])
        model = TextEmbedding(model_name=self.model_name)
        return EmbeddingResult(vectors=[list(vector) for vector in model.embed(texts)])

    def _backend(self) -> str:
        if self.policy is not None:
            return self.policy.backend
        # Deprecated compatibility path, kept only so this change can land on
        # its own: with no injected policy the old development switch still
        # decides. Every production path goes through the policy.
        return (
            FASTEMBED
            if os.environ.get("MEMORY_NET_USE_FASTEMBED") == "1"
            else TEST_CHEAP
        )


def _cheap_vector(text: str) -> list[float]:
    words = text.lower().split()
    return [
        float(len(words)),
        float(len(set(words))),
        float(sum(len(word) for word in words)),
    ]
