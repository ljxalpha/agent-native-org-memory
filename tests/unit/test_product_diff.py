import os
import subprocess
from pathlib import Path

import pytest

from scripts.product_diff import diff_runs


def test_product_diff_between_run_product_dirs(tmp_path: Path) -> None:
    base = tmp_path / "distill_runs" / "a" / "team" / "common"
    head = tmp_path / "distill_runs" / "b" / "team" / "common"
    base.mkdir(parents=True)
    head.mkdir(parents=True)
    (base / "note.md").write_text("old\n", encoding="utf-8")
    (head / "note.md").write_text("new\n", encoding="utf-8")

    diff = diff_runs(tmp_path, "a", "b")

    assert "-old" in diff
    assert "+new" in diff


def test_product_diff_fails_when_run_snapshot_missing(tmp_path: Path) -> None:
    (tmp_path / "team" / "common").mkdir(parents=True)
    (tmp_path / "team" / "common" / "note.md").write_text(
        "current\n",
        encoding="utf-8",
    )

    with pytest.raises(FileNotFoundError, match="missing"):
        diff_runs(tmp_path, "missing", "also-missing")


def test_product_diff_reads_release_branch_snapshots(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _configure(tmp_path)
    (tmp_path / "README.md").write_text("repo\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "seed")
    _add_release_snapshot(tmp_path, "a", "old\n")
    _add_release_snapshot(tmp_path, "b", "new\n")
    _git(tmp_path, "switch", "master")

    diff = diff_runs(tmp_path, "a", "b")

    assert "-old" in diff
    assert "+new" in diff


def test_product_diff_reads_origin_release_branch_snapshots(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "clone", str(remote), str(seed))
    _configure(seed)
    (seed / "README.md").write_text("repo\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "push", "origin", "master")
    _add_release_snapshot(seed, "a", "old\n")
    _git(seed, "push", "origin", "release/a")
    _add_release_snapshot(seed, "b", "new\n")
    _git(seed, "push", "origin", "release/b")
    _git(tmp_path, "clone", str(remote), str(repo))
    _configure(repo)
    _git(repo, "fetch", "--all")

    diff = diff_runs(repo, "a", "b")

    assert "-old" in diff
    assert "+new" in diff


def test_product_diff_explicit_ref_overrides_stale_local_snapshot(
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init")
    _configure(tmp_path)
    (tmp_path / "README.md").write_text("repo\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "seed")
    _add_release_snapshot(tmp_path, "a", "old-from-ref\n")
    _add_release_snapshot(tmp_path, "b", "new-from-ref\n")
    _git(tmp_path, "switch", "master")
    stale = tmp_path / "distill_runs" / "a" / "team" / "common"
    stale.mkdir(parents=True)
    (stale / "note.md").write_text("stale-local\n", encoding="utf-8")

    diff = diff_runs(
        tmp_path,
        "a",
        "b",
        base_ref="release/a",
        head_ref="release/b",
    )

    assert "old-from-ref" in diff
    assert "stale-local" not in diff


def _add_release_snapshot(repo: Path, run_id: str, content: str) -> None:
    _git(repo, "switch", "--orphan", f"release/{run_id}")
    (repo / "README.md").unlink(missing_ok=True)
    product = repo / "distill_runs" / run_id / "team" / "common" / "note.md"
    product.parent.mkdir(parents=True)
    product.write_text(content, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", f"release {run_id}")


def _configure(repo: Path) -> None:
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")


def _git(cwd: Path, *args: str) -> str:
    env = os.environ.copy()
    env.update({"GIT_TERMINAL_PROMPT": "0"})
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()
