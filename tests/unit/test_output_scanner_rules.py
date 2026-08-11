"""Rule-level tests for the output-side scanner (§5.5.8).

Every leak-shaped literal here is assembled from fragments on purpose: a test
that spells out a home path or a hostname puts one in the repository, which is
the very thing these rules exist to keep out.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from memory_net.distiller.output_scanner import (
    HIGH_ENTROPY_RULE,
    PROSE,
    STRUCTURAL,
    OutputScanner,
    RedactionViolation,
    RuleHit,
    Ruleset,
    scan,
)
from memory_net.shared.redaction import Redactor

PATTERNS_PATH = Path("config/redaction_patterns.yaml")

# Assembled, never written whole (see module docstring).
HOME_PATH = "/" + "home/" + "example-user/notes.md"
MAC_HOME_PATH = "/" + "Users/" + "Example.User/notes.md"
WINDOWS_HOME_PATH = "C:" + "\\Users" + "\\Example.User\\notes.md"
HOSTNAME = "build-box" + "." + "local"

REAL_SHA = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
SHORT_SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"


@pytest.fixture()
def ruleset() -> Ruleset:
    return Ruleset.from_yaml(PATTERNS_PATH)


def _rules(hits: list[RuleHit]) -> set[str]:
    return {hit.rule for hit in hits}


def test_abs_home_path_is_detected_in_prose(ruleset: Ruleset) -> None:
    hits = scan(f"see {HOME_PATH} for details", field_kind=PROSE, ruleset=ruleset)
    assert "abs_home_path" in _rules(hits)


def test_abs_home_path_covers_mac_and_windows_forms(ruleset: Ruleset) -> None:
    for text in (MAC_HOME_PATH, WINDOWS_HOME_PATH):
        assert "abs_home_path" in _rules(scan(text, field_kind=PROSE, ruleset=ruleset))


def test_repo_relative_path_is_not_a_home_path(ruleset: Ruleset) -> None:
    """The allowlist case: the paths our own artifacts are full of."""
    hits = scan(
        "team/best_practice/auth-retry.md and distill_runs/latest/metrics.json",
        field_kind=PROSE,
        ruleset=ruleset,
    )
    assert _rules(hits) == set()


def test_hostname_form_is_detected(ruleset: Ruleset) -> None:
    hits = scan(f"deployed from {HOSTNAME}", field_kind=PROSE, ruleset=ruleset)
    assert "hostname_form" in _rules(hits)


def test_public_domain_is_not_a_hostname_form(ruleset: Ruleset) -> None:
    hits = scan(
        "documented at docs.example.com and example.org",
        field_kind=PROSE,
        ruleset=ruleset,
    )
    assert _rules(hits) == set()


def test_injected_hostname_becomes_a_rule(ruleset: Ruleset) -> None:
    injected = Ruleset.from_yaml(
        PATTERNS_PATH, dynamic_values={"local_hostname": ["dev-laptop-7"]}
    )
    text = "built on dev-laptop-7 last night"
    assert "local_hostname" in _rules(scan(text, field_kind=PROSE, ruleset=injected))
    # Without the injection the same corpus says nothing about this machine.
    assert _rules(scan(text, field_kind=PROSE, ruleset=ruleset)) == set()
    assert injected.dynamic_rule_names == ("local_hostname",)


def test_injected_hostname_value_does_not_change_the_version(
    ruleset: Ruleset,
) -> None:
    """Host facts must not version the corpus, or no two machines could compare
    a recorded bundle against a replayed one."""
    injected = Ruleset.from_yaml(
        PATTERNS_PATH, dynamic_values={"local_hostname": ["dev-laptop-7"]}
    )
    assert injected.version == ruleset.version


def test_high_entropy_blob_fires_on_prose_and_is_exempt_on_structural(
    ruleset: Ruleset,
) -> None:
    """The false-positive test the field typing exists for."""
    for value in (REAL_SHA, SHORT_SHA):
        prose_hits = scan(value, field_kind=PROSE, ruleset=ruleset)
        assert HIGH_ENTROPY_RULE in _rules(prose_hits)
        assert scan(value, field_kind=STRUCTURAL, ruleset=ruleset) == []


def test_ordinary_prose_does_not_trip_the_entropy_rule(ruleset: Ruleset) -> None:
    text = (
        "Retry the OAuth token refresh once with jitter before failing the "
        "request, because the identity provider rate limits bursts of renewals."
    )
    assert _rules(scan(text, field_kind=PROSE, ruleset=ruleset)) == set()


def test_distillation_ids_do_not_trip_the_entropy_rule(ruleset: Ruleset) -> None:
    """Our own identifiers are long dense tokens; firing on them would fail
    every real run rather than catch anything."""
    payload = {
        "products_active": {
            "distill-20260810T101010Z-a1b2c3d4-001": {
                "distillation_id": "distill-20260810T101010Z-a1b2c3d4-001",
                "snapshot_ref": "distill_runs/20260810T101010Z-a1b2c3d4/snapshot.json",
            }
        }
    }
    assert scan(payload, field_kind=PROSE, ruleset=ruleset) == []


def test_nested_tree_is_walked_in_full(ruleset: Ruleset) -> None:
    payload = {
        "clusters": [
            {"members": [{"note": f"wrote {HOME_PATH}"}]},
        ]
    }
    hits = scan(payload, field_kind=PROSE, ruleset=ruleset)
    assert [hit.field_path for hit in hits] == [
        "clusters[0].members[0].note",
    ]
    assert _rules(hits) == {"abs_home_path"}


def test_map_keys_are_scanned_for_paths(ruleset: Ruleset) -> None:
    hits = scan({HOME_PATH: "x"}, field_kind=PROSE, ruleset=ruleset)
    assert _rules(hits) == {"abs_home_path"}
    assert hits[0].field_kind == STRUCTURAL


def test_hit_never_carries_the_matched_text(ruleset: Ruleset) -> None:
    secret = "sk_live_" + "A" * 30
    hits = scan(f"key {secret}", field_kind=PROSE, ruleset=ruleset)
    assert hits
    for hit in hits:
        assert secret not in repr(hit)
        assert secret not in json.dumps(hit.as_dict())


def test_line_numbers_are_reported(ruleset: Ruleset) -> None:
    hits = scan(f"first\nsecond\n{HOME_PATH}", field_kind=PROSE, ruleset=ruleset)
    assert [hit.line for hit in hits] == [3]


def test_unknown_field_kind_is_rejected(ruleset: Ruleset) -> None:
    with pytest.raises(ValueError, match="field kind"):
        scan("x", field_kind="audit", ruleset=ruleset)


def test_blocklist_terms_are_rules_too(ruleset: Ruleset) -> None:
    hits = scan("DO_NOT_REPORT_SECRET", field_kind=PROSE, ruleset=ruleset)
    assert "blocklist:DO_NOT_REPORT_SECRET" in _rules(hits)


def test_threshold_edit_changes_the_version(tmp_path: Path) -> None:
    """The reason this loader exists instead of Redactor.from_yaml."""
    data = yaml.safe_load(PATTERNS_PATH.read_text(encoding="utf-8"))
    baseline = tmp_path / "baseline.yaml"
    baseline.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
    data["output_scanner_thresholds"]["high_entropy_min_length"] = 33
    edited = tmp_path / "edited.yaml"
    edited.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")

    assert Ruleset.from_yaml(baseline).version != Ruleset.from_yaml(edited).version
    # And the leaf-side loader is exactly the one that would not have noticed.
    assert Redactor.from_yaml(baseline).version == Redactor.from_yaml(edited).version


def test_leaf_side_rules_still_compile_and_redact(ruleset: Ruleset) -> None:
    """Regression on the shared corpus: the output side only appended to it."""
    redactor = Redactor.from_yaml(PATTERNS_PATH)
    assert len(redactor.rules) == len(ruleset.rules)
    result = redactor.redact("mail alice@example.com now")
    assert "alice@example.com" not in result.content
    assert result.stats["email"] == 1


def test_scanner_enforce_raises_and_counts(ruleset: Ruleset) -> None:
    scanner = OutputScanner(ruleset, mode="enforce")
    with pytest.raises(RedactionViolation) as excinfo:
        scanner.check(f"see {HOME_PATH}", field_kind=PROSE, field_path="summary")
    assert HOME_PATH not in str(excinfo.value)
    assert scanner.violations == 1
    assert scanner.observations_snapshot() == {"abs_home_path": {PROSE: 1}}


def test_scanner_observe_records_and_returns(ruleset: Ruleset) -> None:
    scanner = OutputScanner(ruleset, mode="observe")
    verdict = scanner.check(f"see {HOME_PATH}", field_kind=PROSE)
    assert not verdict.clean
    assert verdict.mode == "observe"
    assert verdict.ruleset_version == ruleset.version
    assert scanner.violations == 0
    assert scanner.rule_counts == {"abs_home_path": 1}


def test_scanner_clean_payload_returns_a_clean_verdict(ruleset: Ruleset) -> None:
    scanner = OutputScanner(ruleset)
    verdict = scanner.check("nothing to see", field_kind=PROSE)
    assert verdict.clean
    assert scanner.observations_snapshot() == {}


def test_scanner_rejects_leaf_side_mode_vocabulary(ruleset: Ruleset) -> None:
    with pytest.raises(ValueError, match="mode"):
        OutputScanner(ruleset, mode="audit")
