"""The clusterer takes the run's embedding policy (§5.5.7).

The point of moving the decision out of this module is that the embedder makes
the same one: two independent reads of the environment can select different
backends, and a run with real vectors collapsed into a single cluster reports
success with entirely ordinary metrics.
"""

from pathlib import Path
from typing import Any

import pytest

from memory_net.distiller.clustering.dbscan import cluster_vectors
from memory_net.distiller.clustering.policy import EmbeddingPolicy

CHEAP_VECTORS = [[1.0, 1.0, 3.0], [9.0, 9.0, 40.0], [1.0, 1.0, 3.0]]


def test_the_test_backend_keeps_its_deterministic_single_cluster() -> None:
    """CI's hermetic behaviour depends on this exact fallback; changing it
    would silently rewrite every clustering assertion in the suite."""
    labels = cluster_vectors(CHEAP_VECTORS, policy=EmbeddingPolicy("test-cheap"))

    assert labels == [0, 0, 0]


def test_an_empty_input_is_empty_under_every_backend() -> None:
    assert cluster_vectors([], policy=EmbeddingPolicy("fastembed")) == []
    assert cluster_vectors([], policy=EmbeddingPolicy("test-cheap")) == []


def test_the_real_backend_actually_separates_distant_vectors() -> None:
    """Otherwise "fastembed selected" and "single cluster" would be
    indistinguishable from the outside."""
    pytest.importorskip("sklearn")

    labels = cluster_vectors(
        [[0.0, 0.0], [0.05, 0.0], [10.0, 10.0], [10.05, 10.0]],
        eps=0.5,
        min_samples=2,
        policy=EmbeddingPolicy("fastembed"),
    )

    assert labels[0] == labels[1]
    assert labels[2] == labels[3]
    assert labels[0] != labels[2]


def test_a_selected_real_backend_without_sklearn_raises(monkeypatch: Any) -> None:
    """Collapsing into one cluster instead would hand the model a single
    undifferentiated blob and still report a successful run."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("sklearn"):
            raise ImportError("no sklearn here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(RuntimeError, match="scikit-learn"):
        cluster_vectors(CHEAP_VECTORS, policy=EmbeddingPolicy("fastembed"))


# --------------------------------------------------------------------------
# The deprecated compatibility path
# --------------------------------------------------------------------------


def test_without_a_policy_the_old_switch_still_decides(monkeypatch: Any) -> None:
    monkeypatch.delenv("MEMORY_NET_USE_FASTEMBED", raising=False)

    assert cluster_vectors(CHEAP_VECTORS) == [0, 0, 0]


def test_the_embedding_switch_is_read_in_exactly_one_module_per_role() -> None:
    """Each of the two roles keeps one deprecated read, and only one. A third
    read anywhere reopens the disagreement this policy exists to close."""
    clustering = Path(cluster_vectors.__module__.replace(".", "/")).parent
    hits = sorted(
        path.name
        for path in (Path.cwd() / clustering).glob("*.py")
        if 'os.environ.get("MEMORY_NET_USE_FASTEMBED")'
        in path.read_text(encoding="utf-8")
    )

    assert hits == ["dbscan.py", "embedder.py"]
