from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from memory_net.shared.util.hashing import short_hash


@dataclass(frozen=True)
class RedactionRule:
    name: str
    pattern: str
    replacement: str
    action: str = "redact"


@dataclass(frozen=True)
class RedactionResult:
    content: str
    stats: dict[str, int] = field(default_factory=dict)
    blocked: bool = False
    blocked_rules: list[str] = field(default_factory=list)


class Redactor:
    def __init__(self, *, version: str, rules: list[RedactionRule]) -> None:
        self.version = version
        self.rules = rules
        self._compiled: list[tuple[RedactionRule, re.Pattern[str]]] = []
        for rule in rules:
            try:
                regex = re.compile(rule.pattern, re.MULTILINE | re.DOTALL)
            except re.error as exc:
                raise ValueError(
                    f"invalid redaction pattern {rule.name!r}: {exc}"
                ) from exc
            self._compiled.append((rule, regex))

    @classmethod
    def from_yaml(cls, path: Path) -> Redactor:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        rules: list[RedactionRule] = []
        for item in data.get("patterns") or []:
            name = str(item["name"])
            pattern = item.get("regex", item.get("pattern"))
            if pattern is None:
                raise ValueError(f"redaction pattern {name!r} missing regex")
            rules.append(
                RedactionRule(
                    name=name,
                    pattern=str(pattern),
                    replacement=str(item.get("replacement") or f"<REDACTED:{name}>"),
                    action=str(item.get("action") or "redact"),
                )
            )
        for term in data.get("blocklist_terms") or []:
            escaped = re.escape(str(term))
            rules.append(
                RedactionRule(
                    name=f"blocklist:{term}",
                    pattern=escaped,
                    replacement="<BLOCKED>",
                    action="block",
                )
            )
        version = _patterns_version(
            data.get("patterns") or [], data.get("blocklist_terms") or []
        )
        return cls(version=version, rules=rules)

    def redact(self, content: str) -> RedactionResult:
        current = content
        stats = {
            rule.name: len(regex.findall(content))
            for rule, regex in self._compiled
            if regex.search(content)
        }
        blocked_rules = [
            rule.name
            for rule, _regex in self._compiled
            if rule.action == "block" and stats.get(rule.name, 0) > 0
        ]
        if blocked_rules:
            return RedactionResult(
                content="",
                stats=stats,
                blocked=True,
                blocked_rules=blocked_rules,
            )
        for rule, regex in self._compiled:
            if stats.get(rule.name) and rule.action != "block":
                current = regex.sub(rule.replacement, current)
        residual = [
            rule.name
            for rule, regex in self._compiled
            if rule.action != "block" and regex.search(current)
        ]
        if residual:
            raise ValueError(
                "redaction fixed-point failed; residual matches for: "
                + ", ".join(residual)
            )
        return RedactionResult(
            content=current,
            stats=stats,
            blocked=bool(blocked_rules),
            blocked_rules=blocked_rules,
        )


def null_redactor() -> Redactor:
    return Redactor(version="null-redactor", rules=[])


def _patterns_version(patterns: object, blocklist_terms: object) -> str:
    payload = json.dumps(
        {
            "patterns": patterns,
            "blocklist_terms": blocklist_terms,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return f"redact-{short_hash(payload, 12)}"
