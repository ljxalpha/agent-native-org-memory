from __future__ import annotations

import os


def cluster_vectors(
    vectors: list[list[float]],
    *,
    eps: float = 0.5,
    min_samples: int = 2,
) -> list[int]:
    if not vectors:
        return []
    # Real DBSCAN belongs to the opt-in "real ML stack", gated by the same flag as
    # the embedder (MEMORY_NET_USE_FASTEMBED). Clustering the cheap offline vectors
    # with DBSCAN is not meaningful, so without the flag we use a deterministic
    # single-cluster fallback. This keeps tests/CI hermetic regardless of whether
    # scikit-learn happens to be installed (CI installs it; offline runs may not).
    if os.environ.get("MEMORY_NET_USE_FASTEMBED") != "1":
        return [0 for _ in vectors]
    try:
        from sklearn.cluster import DBSCAN
    except ImportError:
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
