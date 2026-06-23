from pathlib import Path

from memory_net.shared.git_io import GitRepo


def test_clone_dry_run_does_not_create_worktree(tmp_path: Path) -> None:
    target = tmp_path / "worktree"

    repo = GitRepo.clone("https://example.invalid/memory.git", target, dry_run=True)

    assert repo.dry_run is True
    assert not target.exists()
