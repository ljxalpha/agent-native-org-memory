"""Output-side leak detection for distillation (§5.5.8).

The leaf side *rewrites* (``memory_net/shared/redaction``); this side only
*decides*. Rewriting model output would mean the bundle no longer holds what the
model actually answered, which is the whole point of recording one -- so a hit
here refuses the write instead of editing it.

Nothing in this module does IO beyond reading the rule file once, and a
``RuleHit`` never carries the matched text: a finding that quotes the secret it
found is itself a place the secret leaks to.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from memory_net.distiller.profile import OUTPUT_SCANNER_MODES
from memory_net.shared.util.hashing import short_hash

PROSE = "prose"
STRUCTURAL = "structural"
FIELD_KINDS = (PROSE, STRUCTURAL)
"""How a value is read, which is what decides how strictly it is scanned.

``structural`` is not "trusted": it is exempt from the entropy rule only. A
content hash is a long high-entropy string by construction, so that one rule
would fire on every citation ever emitted.
"""

STRUCTURAL_FIELD_NAMES = frozenset(
    {
        "blob_sha256",
        "cluster_id",
        "commit",
        "commit_sha",
        "config_revision",
        "content_hash",
        "distillation_id",
        "evidence_hash",
        "id",
        "last_run_id",
        "master_snapshot_sha",
        "memory_id",
        "profile_blob_sha256",
        "prompt_hash",
        "revision",
        "revision_hash",
        "run_id",
        "sha",
        "sha256",
        "snapshot_ref",
        "snapshot_sha",
    }
)
"""Keys whose values are machine identifiers or hashes by contract.

The exemption is bound to the field's meaning, not to the call site: a caller
scanning a whole bundle JSON as ``prose`` must not have to enumerate which of
its nested keys hold hashes. Identifiers are here for the same reason hashes
are -- ``distill-<run_id>-001`` is a long dense token this project mints itself,
so the entropy rule would fire on every product ever written.
"""

HIGH_ENTROPY_RULE = "high_entropy_blob"

DEFAULT_THRESHOLDS: dict[str, float] = {
    "high_entropy_min_length": 32,
    "high_entropy_min_bits_per_char": 3.5,
}

_ENTROPY_ALPHABET = "A-Za-z0-9+/=_-"


class RedactionViolation(RuntimeError):
    """Enforce-mode outcome: the payload must not be persisted or sent.

    Carries the hits so the counts can travel with the failure -- the run-level
    metrics have to state which rules fired, and re-deriving that after the
    exception has unwound is not possible.
    """

    def __init__(self, hits: Sequence[RuleHit]) -> None:
        self.hits = tuple(hits)
        rules = sorted({hit.rule for hit in self.hits})
        # Rule names and counts only. The message reaches logs and health files.
        super().__init__(
            f"output scanner blocked {len(self.hits)} hit(s): {', '.join(rules)}"
        )


@dataclass(frozen=True)
class RuleHit:
    """One rule firing, located but never quoted.

    Shaped after ``memory_net/leaf/scanner.py``'s ``ScannerFinding`` (rule plus
    position, no content) with the two fields the output side needs to explain a
    hit: which field it was in, and how that field was read.
    """

    rule: str
    field_path: str
    field_kind: str
    line: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "field_kind": self.field_kind,
            "field_path": self.field_path,
            "line": self.line,
            "rule": self.rule,
        }


@dataclass(frozen=True)
class ScanVerdict:
    """Proof that a payload was scanned, passed to whatever persists it.

    Exists so a writer can *require* evidence of a scan rather than trusting
    that some caller upstream remembered to run one.
    """

    hits: tuple[RuleHit, ...]
    ruleset_version: str
    mode: str

    @property
    def clean(self) -> bool:
        return not self.hits


@dataclass(frozen=True)
class _CompiledRule:
    name: str
    regex: re.Pattern[str]


@dataclass(frozen=True)
class Ruleset:
    """The compiled output-side rules plus the version they were loaded at.

    ``version`` covers the four YAML sections this loader reads -- patterns,
    blocklist terms, thresholds and dynamic rules. ``Redactor``'s version covers
    only the first two, so a threshold-only edit would leave it unchanged: two
    different judgements behind one version string.
    """

    version: str
    rules: tuple[_CompiledRule, ...]
    thresholds: Mapping[str, float]
    dynamic_rule_names: tuple[str, ...] = ()

    @classmethod
    def from_yaml(
        cls,
        path: Path,
        *,
        dynamic_values: Mapping[str, Sequence[str]] | None = None,
    ) -> Ruleset:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        patterns = data.get("patterns") or []
        blocklist_terms = data.get("blocklist_terms") or []
        raw_thresholds = data.get("output_scanner_thresholds") or {}
        dynamic_rules = data.get("output_scanner_dynamic_rules") or []

        rules: list[_CompiledRule] = []
        for item in patterns:
            name = str(item["name"])
            pattern = item.get("regex", item.get("pattern"))
            if pattern is None:
                raise ValueError(f"output scanner rule {name!r} missing regex")
            rules.append(_compile(name, str(pattern)))
        for term in blocklist_terms:
            rules.append(_compile(f"blocklist:{term}", re.escape(str(term))))
        dynamic_names = tuple(
            _dynamic_rules(dynamic_rules, dynamic_values or {}, into=rules)
        )
        thresholds = dict(DEFAULT_THRESHOLDS)
        for key, value in dict(raw_thresholds).items():
            thresholds[str(key)] = float(value)
        return cls(
            version=_ruleset_version(
                patterns, blocklist_terms, raw_thresholds, dynamic_rules
            ),
            rules=tuple(rules),
            thresholds=thresholds,
            # Names only. The injected *values* are host facts; folding them into
            # the version would give one rule file two versions on two machines,
            # and a bundle recorded on one would never match the other.
            dynamic_rule_names=dynamic_names,
        )

    @property
    def min_entropy_length(self) -> int:
        return int(self.thresholds["high_entropy_min_length"])

    @property
    def min_entropy_bits(self) -> float:
        return float(self.thresholds["high_entropy_min_bits_per_char"])


def scan(
    payload: str | Mapping[str, Any] | Sequence[Any],
    *,
    field_kind: str,
    ruleset: Ruleset,
    field_path: str = "",
) -> list[RuleHit]:
    """Every rule that fires on ``payload``, in a stable order.

    ``payload`` may be a string or a whole JSON tree; a tree is walked in full,
    including nested lists, because "only the fields we thought of" is how a
    secret rides out inside an unexpected one. ``field_kind`` is the default for
    the tree's leaves -- a leaf whose key is a known hash field overrides it.
    """
    if field_kind not in FIELD_KINDS:
        raise ValueError(f"unsupported field kind: {field_kind!r}")
    hits: list[RuleHit] = []
    _walk(payload, kind=field_kind, path=field_path, ruleset=ruleset, hits=hits)
    return hits


class OutputScanner:
    """One run's scanner: a ruleset, a mode, and the run-level hit counters.

    Constructed once by the CLI and carried on ``RunContext``; every consumer
    reads it from there rather than building its own, so a run cannot end up
    with two scanners disagreeing about the mode.
    """

    def __init__(self, ruleset: Ruleset, *, mode: str = "enforce") -> None:
        if mode not in OUTPUT_SCANNER_MODES:
            raise ValueError(
                f"output scanner mode must be one of {OUTPUT_SCANNER_MODES}, "
                f"got {mode!r}"
            )
        self.ruleset = ruleset
        self.mode = mode
        # rule -> field kind -> count. Never the matched text, in either mode.
        self.observations: dict[str, dict[str, int]] = {}
        self.violations = 0

    @property
    def enforcing(self) -> bool:
        return self.mode == "enforce"

    @property
    def rule_counts(self) -> dict[str, int]:
        return {
            rule: sum(by_kind.values())
            for rule, by_kind in sorted(self.observations.items())
        }

    def observations_snapshot(self) -> dict[str, dict[str, int]]:
        """Stable, JSON-serialisable view for metrics; rule names and counts."""
        return {
            rule: dict(sorted(by_kind.items()))
            for rule, by_kind in sorted(self.observations.items())
        }

    def check(
        self,
        payload: str | Mapping[str, Any] | Sequence[Any],
        *,
        field_kind: str,
        field_path: str = "",
    ) -> ScanVerdict:
        """Scan, count, and in ``enforce`` refuse by raising.

        The count happens before the raise on purpose: the failure path is
        exactly where the run's metrics need to say which rules fired.
        """
        hits = scan(
            payload,
            field_kind=field_kind,
            ruleset=self.ruleset,
            field_path=field_path,
        )
        for hit in hits:
            by_kind = self.observations.setdefault(hit.rule, {})
            by_kind[hit.field_kind] = by_kind.get(hit.field_kind, 0) + 1
        if hits and self.enforcing:
            self.violations += len(hits)
            raise RedactionViolation(hits)
        return ScanVerdict(
            hits=tuple(hits), ruleset_version=self.ruleset.version, mode=self.mode
        )


def _walk(
    value: Any,
    *,
    kind: str,
    path: str,
    ruleset: Ruleset,
    hits: list[RuleHit],
) -> None:
    if isinstance(value, str):
        hits.extend(_scan_text(value, kind=kind, path=path, ruleset=ruleset))
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key)
            child = f"{path}.{name}" if path else name
            # The key itself can carry a home path or a hostname (a leaf ref
            # keyed by branch name, for instance), so it is scanned too -- but
            # as ``structural``: map keys in this project's artifacts are ids
            # (``products_active`` is keyed by ``distillation_id``), and the
            # entropy rule would fire on every one of them. The regex rules,
            # which are what a path or a hostname in a key trips, still apply.
            hits.extend(
                _scan_text(name, kind=STRUCTURAL, path=f"{child}<key>", ruleset=ruleset)
            )
            _walk(
                item,
                kind=STRUCTURAL if name in STRUCTURAL_FIELD_NAMES else kind,
                path=child,
                ruleset=ruleset,
                hits=hits,
            )
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _walk(
                item,
                kind=kind,
                path=f"{path}[{index}]",
                ruleset=ruleset,
                hits=hits,
            )
        return
    # Numbers, booleans and None hold no text to match.


def _scan_text(text: str, *, kind: str, path: str, ruleset: Ruleset) -> list[RuleHit]:
    hits: list[RuleHit] = []
    for rule in ruleset.rules:
        for match in rule.regex.finditer(text):
            hits.append(
                RuleHit(
                    rule=rule.name,
                    field_path=path,
                    field_kind=kind,
                    line=text.count("\n", 0, match.start()) + 1,
                )
            )
    if kind != STRUCTURAL:
        hits.extend(_entropy_hits(text, kind=kind, path=path, ruleset=ruleset))
    return hits


def _entropy_hits(
    text: str, *, kind: str, path: str, ruleset: Ruleset
) -> list[RuleHit]:
    """Long, dense, single-token runs -- keys and blobs, not sentences.

    Measured per token rather than over the whole string: English prose sits
    around 4 bits per character when measured whole, so a string-wide threshold
    flags every paragraph. A 32-character run with no whitespace in it is
    already unusual in prose, and its per-character entropy separates
    ``deploy-window-retry-guidance`` from a base64 key.
    """
    minimum = ruleset.min_entropy_length
    if minimum <= 0:
        return []
    token_re = re.compile(f"[{_ENTROPY_ALPHABET}]{{{minimum},}}")
    hits: list[RuleHit] = []
    for match in token_re.finditer(text):
        if _shannon_bits_per_char(match.group()) >= ruleset.min_entropy_bits:
            hits.append(
                RuleHit(
                    rule=HIGH_ENTROPY_RULE,
                    field_path=path,
                    field_kind=kind,
                    line=text.count("\n", 0, match.start()) + 1,
                )
            )
    return hits


def _shannon_bits_per_char(token: str) -> float:
    counts: dict[str, int] = {}
    for char in token:
        counts[char] = counts.get(char, 0) + 1
    total = len(token)
    return -sum((count / total) * math.log2(count / total) for count in counts.values())


def _compile(name: str, pattern: str) -> _CompiledRule:
    try:
        regex = re.compile(pattern, re.MULTILINE | re.DOTALL)
    except re.error as exc:
        raise ValueError(f"invalid output scanner pattern {name!r}: {exc}") from exc
    return _CompiledRule(name=name, regex=regex)


def _dynamic_rules(
    definitions: Iterable[Any],
    values: Mapping[str, Sequence[str]],
    *,
    into: list[_CompiledRule],
) -> list[str]:
    """Rules whose *values* come from the environment, not from the repository.

    The local hostname is the case this exists for: it is a leak when it appears
    in output, and writing it into a tracked YAML to detect it would be the same
    leak in a different file.
    """
    names: list[str] = []
    for item in definitions:
        name = str(item["name"])
        names.append(name)
        source = str(item.get("values_from") or name)
        template = str(item.get("regex_template") or "{value}")
        for raw in values.get(source, ()):  # empty when nothing was injected
            text = str(raw).strip()
            if not text:
                continue
            into.append(_compile(name, template.format(value=re.escape(text))))
    return names


def _ruleset_version(
    patterns: object,
    blocklist_terms: object,
    thresholds: object,
    dynamic_rules: object,
) -> str:
    payload = json.dumps(
        {
            "blocklist_terms": blocklist_terms,
            "output_scanner_dynamic_rules": dynamic_rules,
            "output_scanner_thresholds": thresholds,
            "patterns": patterns,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return f"outscan-{short_hash(payload, 12)}"
