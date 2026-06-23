from pathlib import Path

import pytest

from memory_net.leaf.reporter import RedactionBlockedError, report
from memory_net.shared.adapters import FileSystemMdAdapter
from memory_net.shared.redaction import Redactor


def test_redactor_redacts_and_blocks(tmp_path: Path) -> None:
    config = tmp_path / "redaction.yaml"
    config.write_text(
        """
version: 3
patterns:
  - name: email
    pattern: '[^ ]+@example\\.com'
    replacement: '<EMAIL>'
  - name: private
    pattern: 'PRIVATE'
    replacement: '<PRIVATE>'
    action: block
""",
        encoding="utf-8",
    )

    result = Redactor.from_yaml(config).redact("hello a@example.com PRIVATE")

    assert result.content == ""
    assert result.stats == {"email": 1, "private": 1}
    assert result.blocked is True
    assert result.blocked_rules == ["private"]


def test_redactor_blocklist_terms_are_escaped(tmp_path: Path) -> None:
    config = tmp_path / "redaction.yaml"
    config.write_text(
        """
blocklist_terms:
  - "secret.value"
""",
        encoding="utf-8",
    )

    result = Redactor.from_yaml(config).redact("secret-value secret.value")

    assert result.blocked is True
    assert result.blocked_rules == ["blocklist:secret.value"]


def test_redactor_rejects_bad_pattern_at_load_time(tmp_path: Path) -> None:
    config = tmp_path / "redaction.yaml"
    config.write_text(
        """
patterns:
  - name: bad
    pattern: '['
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid redaction pattern"):
        Redactor.from_yaml(config)


def test_redactor_supports_regex_field_and_fixed_point(tmp_path: Path) -> None:
    config = tmp_path / "redaction.yaml"
    config.write_text(
        """
patterns:
  - name: token
    regex: 'token=[A-Za-z0-9]+'
    replacement: 'token=<REDACTED>'
""",
        encoding="utf-8",
    )

    result = Redactor.from_yaml(config).redact("token=abcdef")

    assert result.content == "token=<REDACTED>"
    assert result.stats == {"token": 1}


def test_redactor_fails_when_replacement_still_matches(tmp_path: Path) -> None:
    config = tmp_path / "redaction.yaml"
    config.write_text(
        """
patterns:
  - name: token
    regex: 'token=[A-Za-z0-9<>]+'
    replacement: 'token=<REDACTED>'
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="fixed-point"):
        Redactor.from_yaml(config).redact("token=abcdef")


def test_report_raises_when_redaction_blocks(tmp_path: Path) -> None:
    agent_root = tmp_path / "agent"
    shared = agent_root / "memory" / "shared"
    shared.mkdir(parents=True)
    (shared / "note.md").write_text("DO_NOT_REPORT_SECRET", encoding="utf-8")
    adapter_config = tmp_path / "adapter.yaml"
    adapter_config.write_text(
        """
name: claude-code
roots:
  shared:
    - alias: shared
      base_dir: "${AGENT_ROOT}/memory/shared"
      include: ["**/*.md"]
team_target: "${AGENT_ROOT}/memory/team"
""",
        encoding="utf-8",
    )
    redaction_config = tmp_path / "redaction.yaml"
    redaction_config.write_text(
        """
blocklist_terms:
  - DO_NOT_REPORT_SECRET
""",
        encoding="utf-8",
    )

    with pytest.raises(RedactionBlockedError):
        report(
            repo_path=tmp_path / "repo",
            node_id="alice-01",
            adapter=FileSystemMdAdapter.from_yaml(
                adapter_config,
                agent_root=agent_root,
            ),
            redactor=Redactor.from_yaml(redaction_config),
        )
