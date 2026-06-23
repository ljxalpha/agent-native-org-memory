from pathlib import Path

from scripts.validate_shared_pilot_gate import validate


def test_shared_pilot_gate_accepts_review_gate(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / ".github").mkdir()
    (tmp_path / "config" / "distill.yaml").write_text(
        "review_gate:\n  enabled: true\n  codeowners_path: .github/CODEOWNERS\n",
        encoding="utf-8",
    )
    (tmp_path / ".github" / "CODEOWNERS").write_text(
        "/team/ @owners\n", encoding="utf-8"
    )

    assert validate(tmp_path) == 0


def test_shared_pilot_gate_rejects_unprotected_config(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "distill.yaml").write_text(
        "review_gate:\n  enabled: false\n",
        encoding="utf-8",
    )

    assert validate(tmp_path) == 3
