"""The single decision about which embedding backend a run uses (§5.5.7).

Before this existed the choice was made twice, independently, by two modules
each reading ``MEMORY_NET_USE_FASTEMBED`` for itself: the embedder and the
clusterer. Two decisions can disagree -- real vectors clustered by the
single-cluster fallback, say -- and the disagreement has no runtime symptom.
Metrics come out looking ordinary; only the clusters are meaningless.

So the decision is made once, by the CLI, from the profile, and injected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

FASTEMBED: Literal["fastembed"] = "fastembed"
TEST_CHEAP: Literal["test-cheap"] = "test-cheap"
EMBEDDING_BACKENDS = (FASTEMBED, TEST_CHEAP)

DEFAULT_MODEL_NAME = "BAAI/bge-small-en-v1.5"

PROBE_TEXT = "memory net embedding probe"
"""Text embedded to prove the backend really loads.

Content is irrelevant; what matters is that the model is constructed and run
once, at a point in the run where failing costs nothing.
"""


@dataclass(frozen=True)
class EmbeddingPolicy:
    """Which embedding backend to use, and whether it is a real one.

    ``test-cheap`` produces a three-dimensional vector from word counts. It is
    fast, offline and deterministic, which is what CI needs -- and completely
    unrelated to semantic distance, which is what the DBSCAN ``eps`` and
    ``min_samples`` in the profile are tuned for. The two together cluster real
    data into nonsense while every metric looks fine, which is why a run has to
    say out loud which one it used.
    """

    backend: Literal["fastembed", "test-cheap"] = TEST_CHEAP
    model_name: str = DEFAULT_MODEL_NAME

    def __post_init__(self) -> None:
        if self.backend not in EMBEDDING_BACKENDS:
            raise ValueError(
                f"embedding backend must be one of {EMBEDDING_BACKENDS}, "
                f"got {self.backend!r}"
            )
        if self.backend == FASTEMBED and not self.model_name:
            raise ValueError("fastembed requires a model name")

    @classmethod
    def of(cls, backend: str, model_name: str) -> EmbeddingPolicy:
        """Build from plain strings, as a parsed profile supplies them.

        The narrowing lives here rather than at each call site so a profile
        value never has to be cast past the type checker, which is exactly where
        an unchecked backend name would slip through.
        """
        if backend not in EMBEDDING_BACKENDS:
            raise ValueError(
                f"embedding backend must be one of {EMBEDDING_BACKENDS}, "
                f"got {backend!r}"
            )
        narrowed: Literal["fastembed", "test-cheap"] = (
            FASTEMBED if backend == FASTEMBED else TEST_CHEAP
        )
        return cls(backend=narrowed, model_name=model_name)

    @property
    def test_only(self) -> bool:
        """Whether this run's vectors are the stand-in rather than real ones."""
        return self.backend == TEST_CHEAP

    def probe(self) -> None:
        """Load the backend and embed once, or raise.

        Exists so the failure lands early and cheaply. Discovering that the
        model cannot load only when the first cluster is embedded means the run
        has already fetched, read every leaf branch and possibly called the
        model -- all of it wasted, some of it billed.
        """
        from memory_net.distiller.clustering.embedder import FastEmbedder

        result = FastEmbedder(model_name=self.model_name, policy=self).embed(
            [PROBE_TEXT]
        )
        if not result.vectors or not result.vectors[0]:
            raise RuntimeError(
                f"embedding backend {self.backend!r} produced no vector for the probe"
            )
