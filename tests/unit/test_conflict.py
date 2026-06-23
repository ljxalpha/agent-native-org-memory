import pytest

from memory_net.distiller.conflict import normalize_conflicts, preserve_both


def test_preserve_both_deduplicates_positions() -> None:
    assert preserve_both([" a ", "b", "a", ""]) == ["a", "b"]


def test_normalize_conflicts_requires_claims() -> None:
    with pytest.raises(ValueError, match="claim"):
        normalize_conflicts([{"positions": [{"claim": ""}]}])
