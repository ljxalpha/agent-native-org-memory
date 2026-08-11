"""The single embedding-backend decision (§5.5.7).

The failure this guards is silent by construction: three-dimensional word-count
vectors clustered with DBSCAN parameters tuned for semantic distance produce
meaningless products behind entirely ordinary metrics. Nothing at runtime says
so, which is why the decision has to be made once and has to fail loudly.
"""

from typing import Any

import pytest

from memory_net.distiller.clustering.embedder import FastEmbedder
from memory_net.distiller.clustering.policy import (
    DEFAULT_MODEL_NAME,
    EmbeddingPolicy,
)


def test_the_test_backend_produces_the_three_dimensional_stand_in() -> None:
    policy = EmbeddingPolicy(backend="test-cheap")

    vectors = FastEmbedder(policy=policy).embed(["one two two"]).vectors

    assert vectors == [[3.0, 2.0, 9.0]]
    assert policy.test_only is True


def test_an_unknown_backend_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="embedding backend"):
        EmbeddingPolicy(backend="word2vec")  # type: ignore[arg-type]


def test_fastembed_requires_a_model_name() -> None:
    """Without one the backend has nothing to load, and the failure would
    surface as an opaque library error much later."""
    with pytest.raises(ValueError, match="model name"):
        EmbeddingPolicy(backend="fastembed", model_name="")


def test_a_fastembed_policy_is_not_test_only() -> None:
    assert EmbeddingPolicy(backend="fastembed").test_only is False
    assert EmbeddingPolicy().model_name == DEFAULT_MODEL_NAME


# --------------------------------------------------------------------------
# fastembed must not degrade silently
# --------------------------------------------------------------------------


def _break_fastembed(monkeypatch: Any) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "fastembed":
            raise ImportError("no fastembed here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_a_fastembed_policy_raises_rather_than_falling_back(monkeypatch: Any) -> None:
    """The old behaviour returned cheap vectors on ImportError. That produces a
    run whose products look fine and whose clustering is meaningless."""
    _break_fastembed(monkeypatch)

    with pytest.raises(RuntimeError, match="could not be loaded"):
        FastEmbedder(policy=EmbeddingPolicy(backend="fastembed")).embed(["x"])


def test_the_probe_surfaces_a_broken_backend(monkeypatch: Any) -> None:
    """Discovering this at the first cluster means the run has already fetched,
    read every leaf branch and possibly called the model."""
    _break_fastembed(monkeypatch)

    with pytest.raises(RuntimeError, match="could not be loaded"):
        EmbeddingPolicy(backend="fastembed").probe()


def test_the_probe_passes_on_a_working_backend() -> None:
    """A probe that can only fail would stop nothing and prove nothing."""
    EmbeddingPolicy(backend="test-cheap").probe()


def test_the_probe_rejects_an_empty_vector(monkeypatch: Any) -> None:
    from memory_net.distiller.clustering import embedder as embedder_module

    monkeypatch.setattr(
        embedder_module.FastEmbedder,
        "embed",
        lambda self, texts: embedder_module.EmbeddingResult(vectors=[]),
    )

    with pytest.raises(RuntimeError, match="no vector"):
        EmbeddingPolicy(backend="test-cheap").probe()


# --------------------------------------------------------------------------
# The deprecated compatibility path
# --------------------------------------------------------------------------


def test_without_a_policy_the_old_switch_still_decides(monkeypatch: Any) -> None:
    """Kept so this change could land on its own; every production path goes
    through the policy instead."""
    monkeypatch.delenv("MEMORY_NET_USE_FASTEMBED", raising=False)

    assert FastEmbedder().embed(["one two two"]).vectors == [[3.0, 2.0, 9.0]]


def test_without_a_policy_a_broken_fastembed_still_falls_back(
    monkeypatch: Any,
) -> None:
    """The compatibility path must keep its old behaviour exactly, or this
    change would not be independently mergeable."""
    monkeypatch.setenv("MEMORY_NET_USE_FASTEMBED", "1")
    _break_fastembed(monkeypatch)

    assert FastEmbedder().embed(["one two two"]).vectors == [[3.0, 2.0, 9.0]]
