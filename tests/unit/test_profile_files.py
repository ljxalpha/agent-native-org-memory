"""The shipped profiles, checked as configuration rather than as code (§5.5.6).

A profile is the one input that decides whether a run is real, and every failure
it can cause is silent: the run succeeds, the metrics look ordinary, and the
products are worthless. These tests are the only thing standing between an edit
to a YAML file and that outcome.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from memory_net.distiller.output_scanner import (
    Ruleset,
    ScannerSettings,
    resolve_scanner_settings,
)
from memory_net.distiller.profile import (
    PRODUCTION,
    REQUIRED_PRODUCTION_KEYS,
    ProfileError,
    profile_class,
    validate_profile,
)

DEFAULT_PROFILE = Path("config/distill.yaml")
PRODUCTION_PROFILE = Path("config/distill.deepseek.yaml")

#: Keys whose value is a template with a substitution point in it. `<run_id>` in
#: `release/<run_id>` is filled at run time; every *other* `<...>` in a profile
#: is an unfilled blank.
TEMPLATE_KEY_SUFFIX = "_template"


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture()
def production() -> dict[str, Any]:
    return _load(PRODUCTION_PROFILE)


@pytest.fixture()
def default_profile() -> dict[str, Any]:
    return _load(DEFAULT_PROFILE)


def test_the_production_profile_validates(production: dict[str, Any]) -> None:
    assert validate_profile(production) == PRODUCTION


def test_the_production_profile_declares_every_required_key(
    production: dict[str, Any],
) -> None:
    for key in REQUIRED_PRODUCTION_KEYS:
        value: Any = production
        for part in key.split("."):
            assert isinstance(value, dict), key
            assert part in value, key
            value = value[part]
        assert value not in (None, ""), key


def test_the_production_profile_holds_no_placeholders(
    production: dict[str, Any],
) -> None:
    """A `<...>` blank starts a run that behaves as if it were configured."""
    blanks = [
        f"{path}={value}"
        for path, value in _scalars(production)
        if "<" in str(value) and not path.endswith(TEMPLATE_KEY_SUFFIX)
    ]

    assert blanks == []


def test_the_production_profile_names_no_local_identity(
    production: dict[str, Any],
) -> None:
    """No home directories, hostnames or real addresses in a tracked file."""
    text = PRODUCTION_PROFILE.read_text(encoding="utf-8")
    hits = [
        hit
        for hit in _scalars(production)
        if str(hit[1]).startswith("/") and "per_mtok" not in hit[0]
    ]

    assert hits == []
    assert "@" not in text.replace("--", "")


def test_the_production_pricing_is_not_the_anthropic_default(
    production: dict[str, Any], default_profile: dict[str, Any]
) -> None:
    """Pricing in the wrong dimensions is a ceiling that stops the run early or
    lets it overspend; either way the number it reports is fiction."""
    production_pricing = production["llm"]["pricing"]
    default_pricing = default_profile["llm"]["pricing"]

    assert production_pricing != default_pricing
    for key in ("input_per_mtok_usd", "output_per_mtok_usd"):
        assert 0 < production_pricing[key] < default_pricing[key]


def test_the_production_profile_uses_a_real_embedding_model(
    production: dict[str, Any],
) -> None:
    clustering = production["clustering"]

    assert clustering["embedding_backend"] == "fastembed"
    assert clustering["embedding_model"]


def test_the_production_scanner_is_enforcing_and_its_rules_exist(
    production: dict[str, Any],
) -> None:
    settings = resolve_scanner_settings(production, declared_class=PRODUCTION)

    assert settings == ScannerSettings(
        mode="enforce", patterns_path="config/redaction_patterns.yaml"
    )
    # The file it names has to load, or the run stops at startup.
    assert Ruleset.from_yaml(Path(settings.patterns_path)).rules


def test_the_default_profile_is_still_a_development_profile(
    default_profile: dict[str, Any],
) -> None:
    """It carries no profile_class, so none of the production checks apply and
    CI keeps running exactly what it ran."""
    assert "profile_class" not in default_profile
    assert profile_class(default_profile) != PRODUCTION
    assert validate_profile(default_profile) != PRODUCTION


def test_the_default_profile_states_that_its_vectors_are_a_stand_in(
    default_profile: dict[str, Any],
) -> None:
    assert default_profile["clustering"]["embedding_backend"] == "test-cheap"


def test_the_default_profile_would_be_refused_as_production(
    default_profile: dict[str, Any],
) -> None:
    """The check that gives the previous test its meaning: this profile is not
    one somebody can promote by adding a line."""
    promoted = dict(default_profile)
    promoted["profile_class"] = PRODUCTION

    with pytest.raises(ProfileError, match="missing required keys"):
        validate_profile(promoted)


def _scalars(value: Any, path: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        return [
            item
            for key, child in value.items()
            for item in _scalars(child, f"{path}.{key}" if path else str(key))
        ]
    if isinstance(value, list):
        return [
            item
            for index, child in enumerate(value)
            for item in _scalars(child, f"{path}[{index}]")
        ]
    return [(path, value)]
