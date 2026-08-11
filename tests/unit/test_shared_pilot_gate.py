"""The shared-pilot gate: a configuration check that says what it did not check.

The test that matters most is the last kind: with no platform token the script
exits 0 *and* prints that the platform side was not verified. A green run that
did not say so would be read as evidence of protection nobody looked at.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.validate_shared_pilot_gate import (
    NOT_VERIFIED,
    PLATFORM_TOKEN_ENV,
    validate,
)

DISTILL = """review_gate:
  enabled: true
  codeowners_path: .github/CODEOWNERS
  pr_target_branch: master
"""
PLATFORM = """branch_protection:
  protected_branch: master
  master_writers_only:
    - distiller-ci
"""


def _repo(tmp_path: Path, *, distill: str = DISTILL, platform: str = PLATFORM) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / ".github").mkdir()
    (tmp_path / "config" / "distill.yaml").write_text(distill, encoding="utf-8")
    (tmp_path / "config" / "platform.yaml").write_text(platform, encoding="utf-8")
    (tmp_path / ".github" / "CODEOWNERS").write_text(
        "/team/ @owners\n", encoding="utf-8"
    )
    return tmp_path


def test_a_coherent_configuration_passes(tmp_path: Path) -> None:
    assert validate(_repo(tmp_path), environ={}) == 0


def test_an_unprotected_configuration_is_refused(tmp_path: Path) -> None:
    root = _repo(tmp_path, distill="review_gate:\n  enabled: false\n")

    assert validate(root, environ={}) == 3


def test_an_empty_codeowners_is_refused(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / ".github" / "CODEOWNERS").write_text("\n", encoding="utf-8")

    assert validate(root, environ={}) == 4


@pytest.mark.parametrize(
    "platform",
    [
        "branch_protection:\n  protected_branch: master\n  master_writers_only: []\n",
        "branch_protection:\n  protected_branch: master\n",
        "runtime:\n  supported_os: [Linux]\n",
    ],
)
def test_an_empty_writers_list_is_refused(tmp_path: Path, platform: str) -> None:
    assert validate(_repo(tmp_path, platform=platform), environ={}) == 5


def test_a_protected_branch_that_is_not_the_pr_target_is_refused(
    tmp_path: Path,
) -> None:
    root = _repo(
        tmp_path,
        platform=(
            "branch_protection:\n"
            "  protected_branch: main\n"
            "  master_writers_only:\n    - distiller-ci\n"
        ),
    )

    assert validate(root, environ={}) == 6


def test_without_a_token_it_says_it_did_not_verify_the_platform(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert validate(_repo(tmp_path), environ={}) == 0

    out = capsys.readouterr().out
    assert "platform-side protection NOT empirically verified" in out
    assert NOT_VERIFIED in out


def test_a_token_without_a_probe_still_says_it_did_not_verify(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = validate(_repo(tmp_path), environ={PLATFORM_TOKEN_ENV: "t"}, probe=None)

    assert code == 0
    assert "NOT empirically verified" in capsys.readouterr().out


def test_a_probe_that_finds_nothing_reports_a_confirmed_protection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[tuple[str, str]] = []

    def probe(branch: str, token: str) -> list[str]:
        seen.append((branch, token))
        return []

    code = validate(_repo(tmp_path), environ={PLATFORM_TOKEN_ENV: "t"}, probe=probe)

    out = capsys.readouterr().out
    assert code == 0
    assert seen == [("master", "t")]
    assert "platform probe confirms protection on master" in out
    assert "NOT empirically verified" not in out


def test_a_probe_finding_fails_the_gate(tmp_path: Path) -> None:
    code = validate(
        _repo(tmp_path),
        environ={PLATFORM_TOKEN_ENV: "t"},
        probe=lambda branch, token: ["master accepts direct pushes"],
    )

    assert code == 7


def test_the_shipped_configuration_is_coherent() -> None:
    """The repository's own config must satisfy the check it ships."""
    assert validate(Path("."), environ={}) == 0
