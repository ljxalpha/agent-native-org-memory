"""Profile validation and provenance (§5.5.6).

Every value a production profile is forced to declare has a default elsewhere
in the CLI that is right for a mock run and wrong for a real one. Each of those
failures is silent -- the run succeeds, the metrics look ordinary, the products
are worthless -- so refusing to start is the only signal available. These tests
pin one refusal per required key, because a check that covers six of seven
looks identical to one that covers all seven.
"""

from typing import Any

import pytest

from memory_net.distiller.profile import (
    DEVELOPMENT,
    PRODUCTION,
    REQUIRED_PRODUCTION_KEYS,
    ProfileError,
    ProfileProvenance,
    profile_class,
    validate_effective_provider,
    validate_profile,
)


def _production() -> dict[str, Any]:
    """A minimally complete production profile."""
    return {
        "profile_class": PRODUCTION,
        "llm": {
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "budget_usd": 2.0,
            "pricing": {
                "input_per_mtok_usd": 0.27,
                "output_per_mtok_usd": 1.1,
            },
        },
        "clustering": {
            "embedding_backend": "fastembed",
            "embedding_model": "BAAI/bge-small-en-v1.5",
        },
        "output_scanner": {
            "schema_version": "memory-net/output-scanner/v1",
            "mode": "enforce",
        },
    }


def _without(config: dict[str, Any], dotted: str) -> dict[str, Any]:
    """The same profile with one dotted key removed."""
    import copy

    clone = copy.deepcopy(config)
    current: Any = clone
    parts = dotted.split(".")
    for part in parts[:-1]:
        current = current[part]
    del current[parts[-1]]
    return clone


# --------------------------------------------------------------------------
# profile_class
# --------------------------------------------------------------------------


def test_an_absent_class_is_development() -> None:
    """The shipped default profile has no such key and must keep working."""
    assert profile_class({}) == DEVELOPMENT
    assert validate_profile({}) == DEVELOPMENT


def test_an_unknown_class_is_refused() -> None:
    with pytest.raises(ProfileError, match="profile_class"):
        profile_class({"profile_class": "prod"})


# --------------------------------------------------------------------------
# Production has no defaults
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key", REQUIRED_PRODUCTION_KEYS)
def test_a_production_profile_missing_any_required_key_is_refused(key: str) -> None:
    with pytest.raises(ProfileError, match=key):
        validate_profile(_without(_production(), key))


def test_an_explicit_null_counts_as_absent() -> None:
    """``budget_usd:`` with no value declares nothing; accepting it would let a
    production profile satisfy the check with an empty promise."""
    config = _production()
    config["llm"]["budget_usd"] = None

    with pytest.raises(ProfileError, match="llm.budget_usd"):
        validate_profile(config)


def test_a_complete_production_profile_is_accepted() -> None:
    """The other half: a validator that refuses everything guards nothing."""
    assert validate_profile(_production()) == PRODUCTION


def test_the_missing_key_message_names_every_gap_not_just_the_first() -> None:
    """Fixing them one round-trip at a time is how a config change takes an
    afternoon."""
    config = _without(_without(_production(), "llm.model"), "llm.budget_usd")

    with pytest.raises(ProfileError) as raised:
        validate_profile(config)

    assert "llm.model" in str(raised.value)
    assert "llm.budget_usd" in str(raised.value)


# --------------------------------------------------------------------------
# Value constraints
# --------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["mock", "hermes_tool"])
def test_a_non_production_backend_is_refused_in_production(provider: str) -> None:
    """mock is not a model at all, and the bridge loses the system/user role
    boundary on the wire -- not optional when the input is untrusted records."""
    config = _production()
    config["llm"]["provider"] = provider

    with pytest.raises(ProfileError, match="llm.provider"):
        validate_profile(config)


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_string_counts_as_absent(value: str) -> None:
    """``llm.provider: ""`` would otherwise pass the presence check and then be
    resolved to the mock backend downstream."""
    config = _production()
    config["llm"]["provider"] = value

    with pytest.raises(ProfileError, match="llm.provider"):
        validate_profile(config)


@pytest.mark.parametrize("provider", ["mock", "hermes_tool"])
def test_the_provider_a_run_will_really_use_is_checked_too(provider: str) -> None:
    """--provider overrides the profile, so validating the declaration alone
    lets a production run be redirected to a backend that never touches a
    model while still publishing its output as team knowledge."""
    with pytest.raises(ProfileError, match="llm.provider"):
        validate_effective_provider(PRODUCTION, provider=provider, fallback="deepseek")


def test_the_fallback_lane_is_checked_as_well() -> None:
    """It handles the first parse failure, so a mock there quietly substitutes
    fabricated products for the real ones."""
    with pytest.raises(ProfileError, match="fallback"):
        validate_effective_provider(PRODUCTION, provider="deepseek", fallback="mock")


def test_the_effective_check_is_inert_outside_production() -> None:
    validate_effective_provider(DEVELOPMENT, provider="mock", fallback="mock")
    validate_effective_provider(PRODUCTION, provider="deepseek", fallback="none")


def test_the_test_embedding_backend_is_refused_in_production() -> None:
    """Ruled out by what the backend is, not by what the profile is called: the
    stand-in emits three-dimensional word counts while the DBSCAN parameters
    beside it are tuned for semantic distance."""
    config = _production()
    config["clustering"]["embedding_backend"] = "test-cheap"

    with pytest.raises(ProfileError, match="embedding_backend"):
        validate_profile(config)


def test_an_unknown_embedding_backend_is_refused() -> None:
    config = _production()
    config["clustering"]["embedding_backend"] = "word2vec"

    with pytest.raises(ProfileError, match="embedding_backend"):
        validate_profile(config)


def test_the_embedding_model_has_no_production_default() -> None:
    """Defaulting it would pick a model, and therefore an embedding space, on
    the operator's behalf."""
    with pytest.raises(ProfileError, match="clustering.embedding_model"):
        validate_profile(_without(_production(), "clustering.embedding_model"))


def test_production_refuses_observe_rather_than_correcting_it() -> None:
    """A silent correction leaves the author believing the mode they wrote is
    the one in effect."""
    config = _production()
    config["output_scanner"]["mode"] = "observe"

    with pytest.raises(ProfileError, match="enforce"):
        validate_profile(config)

    # And really refused, not rewritten.
    assert config["output_scanner"]["mode"] == "observe"


@pytest.mark.parametrize("mode", ["audit", "warn", "block"])
def test_the_leaf_side_vocabulary_is_refused_in_any_profile_class(mode: str) -> None:
    """Sharing a vocabulary with the leaf scanner is how someone eventually
    reads its ``audit`` as this side's ``observe``. Checked outside production
    too: an unchallenged one in a development profile gets copied later."""
    with pytest.raises(ProfileError, match="output_scanner.mode"):
        validate_profile({"output_scanner": {"mode": mode}})


def test_observe_is_allowed_outside_production() -> None:
    """It exists for calibrating the rule set, which is not a production run."""
    assert validate_profile({"output_scanner": {"mode": "observe"}}) == DEVELOPMENT


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------


def test_provenance_hashes_the_raw_bytes_not_the_parsed_mapping() -> None:
    """Parsing discards comments, key order and formatting, so two visibly
    different files would otherwise carry identical provenance."""
    first = ProfileProvenance.of(
        profile_path="config/distill.yaml",
        raw="llm:\n  provider: mock\n",
        master_snapshot_sha="abc",
    )
    second = ProfileProvenance.of(
        profile_path="config/distill.yaml",
        raw="# a comment\nllm:\n  provider: mock\n",
        master_snapshot_sha="abc",
    )

    assert first.blob_sha256 != second.blob_sha256
    assert len(first.blob_sha256) == 64


def test_provenance_without_a_snapshot_records_an_empty_string() -> None:
    """It lands in JSON beside other string fields, where a null would read as
    "unknown" rather than "there was no snapshot"."""
    provenance = ProfileProvenance.of(
        profile_path="config/distill.yaml", raw="x", master_snapshot_sha=None
    )

    assert provenance.master_snapshot_sha == ""


def test_provenance_dict_keys_are_pinned() -> None:
    """They are written into snapshot.json and metrics.json; renaming one
    silently breaks whoever reads those."""
    provenance = ProfileProvenance.of(
        profile_path="config/profiles/production.yaml",
        raw="x",
        master_snapshot_sha="deadbeef",
    )

    assert provenance.as_dict() == {
        "master_snapshot_sha": "deadbeef",
        "profile_blob_sha256": provenance.blob_sha256,
        "profile_path": "config/profiles/production.yaml",
    }
