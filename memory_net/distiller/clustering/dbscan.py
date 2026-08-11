from __future__ import annotations

import os

from memory_net.distiller.clustering.policy import (
    FASTEMBED,
    TEST_CHEAP,
    EmbeddingPolicy,
)


def cluster_vectors(
    vectors: list[list[float]],
    *,
    eps: float = 0.5,
    min_samples: int = 2,
    policy: EmbeddingPolicy | None = None,
) -> list[int]:
    """Group vectors, using the backend the run's policy selected (§5.5.7).

    Real DBSCAN belongs with real embeddings: ``eps`` and ``min_samples`` are
    tuned for semantic distance, and running them over the three-dimensional
    word-count stand-in produces groupings unrelated to meaning. Under
    ``test-cheap`` this returns one deterministic cluster instead, which is what
    keeps CI hermetic whether or not scikit-learn happens to be installed.

    The policy is passed in rather than re-derived here: this module and the
    embedder must reach the *same* answer, and two independent reads of the
    environment can disagree with nothing at runtime noticing.
    """
    if not vectors:
        return []
    if _backend(policy) == TEST_CHEAP:
        return [0 for _ in vectors]
    try:
        from sklearn.cluster import DBSCAN
    except ImportError as exc:
        if policy is not None:
            # A policy naming fastembed selected the real stack. Collapsing
            # every record into one cluster instead would hand the model a
            # single undifferentiated blob and still report a successful run.
            raise RuntimeError(
                f"embedding backend {FASTEMBED!r} needs scikit-learn for "
                f"clustering: {type(exc).__name__}"
            ) from exc
        return [0 for _ in vectors]
    labels = DBSCAN(eps=eps, min_samples=min_samples).fit(vectors).labels_
    normalized = [int(label) for label in labels]
    next_noise_label = (max(normalized) + 1) if normalized else 0
    result: list[int] = []
    for label in normalized:
        if label >= 0:
            result.append(label)
            continue
        result.append(next_noise_label)
        next_noise_label += 1
    return result


def _backend(policy: EmbeddingPolicy | None) -> str:
    if policy is not None:
        return policy.backend
    # Deprecated compatibility path, mirroring the embedder's: with no injected
    # policy the old development switch still decides. Every production path
    # goes through the policy.
    if os.environ.get("MEMORY_NET_USE_FASTEMBED") == "1":
        return FASTEMBED
    return TEST_CHEAP
