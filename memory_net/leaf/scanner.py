from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from memory_net.leaf.health import utc_now


@dataclass(frozen=True)
class ScannerFinding:
    scanner: str
    rule: str
    relative_path: str
    line: int


@dataclass(frozen=True)
class ScannerAuditResult:
    findings: list[ScannerFinding]

    @property
    def hit_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[finding.scanner] = counts.get(finding.scanner, 0) + 1
        return counts

    @property
    def rule_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for finding in self.findings:
            key = f"{finding.scanner}:{finding.rule}"
            counts[key] = counts.get(key, 0) + 1
        return counts


@dataclass(frozen=True)
class ScannerRule:
    name: str
    pattern: str


BUILTIN_RULES = (
    ScannerRule(
        name="generic_secret_assignment",
        pattern=r"(?i)(password|secret|token|api[_-]?key)\s*[:=]\s*['\"]?[A-Za-z0-9._~+/=-]{12,}",
    ),
    ScannerRule(
        name="private_key_marker",
        pattern=r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----",
    ),
)


def scanner_config_from_report_config(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("external_scanners")
    return raw if isinstance(raw, dict) else {}


def scanner_mode(config: dict[str, Any]) -> str:
    builtin = config.get("builtin")
    mode = builtin.get("mode") if isinstance(builtin, dict) else None
    value = str(mode or "audit").strip().lower()
    if value not in {"audit", "warn", "block"}:
        raise ValueError(f"unsupported scanner mode: {value}")
    return value


def scanner_audit(
    records: list[tuple[str, str]],
    config: dict[str, Any],
) -> ScannerAuditResult:
    findings: list[ScannerFinding] = []
    builtin = config.get("builtin")
    enabled = True
    if isinstance(builtin, dict) and builtin.get("enabled") is not None:
        enabled = bool(builtin["enabled"])
    if not enabled:
        return ScannerAuditResult(findings=[])
    compiled = [
        (rule.name, re.compile(rule.pattern, re.MULTILINE)) for rule in BUILTIN_RULES
    ]
    for relative_path, content in records:
        for rule_name, regex in compiled:
            for match in regex.finditer(content):
                findings.append(
                    ScannerFinding(
                        scanner="builtin",
                        rule=rule_name,
                        relative_path=relative_path,
                        line=content.count("\n", 0, match.start()) + 1,
                    )
                )
    return ScannerAuditResult(findings=findings)


def write_scanner_audit(
    state_dir: Path,
    *,
    node_id: str,
    result: ScannerAuditResult,
) -> Path:
    audit_dir = state_dir / "scanner_audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / f"{utc_now()[:10]}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        for finding in result.findings:
            handle.write(
                json.dumps(
                    {
                        "schema_version": "memory-net/scanner-audit/v1",
                        "node_id": node_id,
                        "found_at": utc_now(),
                        "scanner": finding.scanner,
                        "rule": finding.rule,
                        "relative_path": finding.relative_path,
                        "line": finding.line,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
    return path
