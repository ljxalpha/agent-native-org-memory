from __future__ import annotations

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
        # Resolved once, here, and never re-read. A policy read at each use can
        # differ from the one the vectors were produced under, and the clusterer
        # reads this attribute back to stay in step.
        self.policy = (
            policy if policy is not None else EmbeddingPolicy.from_environment()
        )
        # Strict only when a policy was actually injected. Without one the
        # caller predates the policy and keeps its old degrade-on-import-error
        # behaviour, which is what let this land one step at a time.
        self.strict = policy is not None
        # An explicit model_name still wins, so the pre-policy call sites keep
        # working unchanged.
        self.model_name = model_name or self.policy.model_name

    def embed(self, texts: list[str]) -> EmbeddingResult:
        if self.policy.backend == TEST_CHEAP:
            return EmbeddingResult(vectors=[_cheap_vector(text) for text in texts])
        try:
            from fastembed import TextEmbedding
        except (ImportError, OSError, RuntimeError) as exc:
            if self.strict:
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


def _cheap_vector(text: str) -> list[float]:
    words = text.lower().split()
    return [
        float(len(words)),
        float(len(set(words))),
        float(sum(len(word) for word in words)),
    ]
