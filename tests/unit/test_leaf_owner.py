import json
import os
import subprocess
from pathlib import Path

import pytest

from memory_net.leaf.cli import _commit_and_push_report, _prepare_leaf_branch


def test_prepare_leaf_branch_writes_owner_when_remote_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    repo = tmp_path / "repo"
    home = tmp_path / "mn-home"
    (home / "state").mkdir(parents=True)
    (home / "local.yaml").write_text(
        "agent: codex\nagent_root: /tmp/codex\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MEMORY_NET_HOME", str(home))

    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "clone", str(remote), str(seed))
    _configure(seed)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "push", "origin", "master")
    _git(tmp_path, "clone", str(remote), str(repo))
    _configure(repo)

    _prepare_leaf_branch(repo, "alice")

    owner = json.loads((repo / ".owner.json").read_text(encoding="utf-8"))
    assert owner["schema_version"] == "memory-net/owner/v1"
    assert owner["node_id"] == "alice"
    assert owner["install_id"].startswith("inst-")
    assert owner["agent"] == "codex"


def test_first_report_commit_pushes_owner_when_remote_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    repo = tmp_path / "repo"
    home = tmp_path / "mn-home"
    monkeypatch.setenv("MEMORY_NET_HOME", str(home))

    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "clone", str(remote), str(seed))
    _configure(seed)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "push", "origin", "master")
    _git(tmp_path, "clone", str(remote), str(repo))
    _configure(repo)

    _prepare_leaf_branch(repo, "alice")
    (repo / "manifest.json").write_text("{}\n", encoding="utf-8")
    (repo / ".health.json").write_text("{}\n", encoding="utf-8")

    assert _commit_and_push_report(repo, "alice", no_push=False) is True

    _git(repo, "fetch", "origin")
    remote_owner = _git(repo, "show", "origin/leaf/alice:.owner.json")
    assert json.loads(remote_owner)["node_id"] == "alice"


def test_prepare_leaf_branch_fails_when_remote_owner_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    repo = tmp_path / "repo"
    home = tmp_path / "mn-home"
    monkeypatch.setenv("MEMORY_NET_HOME", str(home))
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "clone", str(remote), str(seed))
    _configure(seed)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "push", "origin", "master")
    _git(seed, "switch", "--orphan", "leaf/alice")
    _git(seed, "rm", "-rf", ".", check=False)
    (seed / "manifest.json").write_text("{}\n", encoding="utf-8")
    _git(seed, "add", "manifest.json")
    _git(seed, "commit", "-m", "ownerless leaf")
    _git(seed, "push", "origin", "leaf/alice")
    _git(tmp_path, "clone", str(remote), str(repo))
    _configure(repo)

    with pytest.raises(RuntimeError, match="missing .owner.json"):
        _prepare_leaf_branch(repo, "alice")


def test_prepare_leaf_branch_repairs_local_unpublished_leaf_branch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    repo = tmp_path / "repo"
    home = tmp_path / "mn-home"
    monkeypatch.setenv("MEMORY_NET_HOME", str(home))
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "clone", str(remote), str(seed))
    _configure(seed)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "push", "origin", "master")
    _git(tmp_path, "clone", str(remote), str(repo))
    _configure(repo)
    _git(repo, "switch", "--orphan", "leaf/alice")
    _git(repo, "rm", "-rf", ".", check=False)

    _prepare_leaf_branch(repo, "alice")

    assert (
        json.loads((repo / ".owner.json").read_text(encoding="utf-8"))["node_id"]
        == "alice"
    )


def test_commit_allows_same_install_fingerprint_drift(
    tmp_path: Path,
    monkeypatch,
) -> None:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    repo = tmp_path / "repo"
    home = tmp_path / "mn-home"
    monkeypatch.setenv("MEMORY_NET_HOME", str(home))
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "clone", str(remote), str(seed))
    _configure(seed)
    _git(seed, "switch", "--orphan", "leaf/alice")
    owner = {
        "schema_version": "memory-net/owner/v1",
        "node_id": "alice",
        "install_id": "inst-shared",
        "owner_fingerprint": "remote-fp",
        "host": "remote-host",
    }
    (seed / ".owner.json").write_text(json.dumps(owner), encoding="utf-8")
    (seed / "manifest.json").write_text("{}\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "leaf owner")
    _git(seed, "push", "origin", "leaf/alice")
    _git(tmp_path, "clone", str(remote), str(repo))
    _configure(repo)
    _git(repo, "switch", "-C", "leaf/alice", "origin/leaf/alice")
    local_owner = owner | {"owner_fingerprint": "local-fp"}
    (repo / ".owner.json").write_text(json.dumps(local_owner), encoding="utf-8")
    (repo / ".health.json").write_text("{}\n", encoding="utf-8")

    assert _commit_and_push_report(repo, "alice", no_push=False) is True


def test_prepare_leaf_branch_rejects_fresh_clone_wrong_install(
    tmp_path: Path,
    monkeypatch,
) -> None:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    repo = tmp_path / "repo"
    home = tmp_path / "mn-home"
    monkeypatch.setenv("MEMORY_NET_HOME", str(home))
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "clone", str(remote), str(seed))
    _configure(seed)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "push", "origin", "master")
    _git(seed, "switch", "--orphan", "leaf/alice")
    _git(seed, "rm", "-rf", ".", check=False)
    (seed / ".owner.json").write_text(
        json.dumps(
            {
                "schema_version": "memory-net/owner/v1",
                "node_id": "alice",
                "install_id": "inst-remote",
                "owner_fingerprint": "remote-fp",
                "host": "remote-host",
            }
        ),
        encoding="utf-8",
    )
    (seed / "manifest.json").write_text("{}\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "leaf owner")
    _git(seed, "push", "origin", "leaf/alice")
    _git(tmp_path, "clone", str(remote), str(repo))
    _configure(repo)

    with pytest.raises(RuntimeError, match="owner install_id changed"):
        _prepare_leaf_branch(repo, "alice")


def _configure(repo: Path) -> None:
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")


def _git(cwd: Path, *args: str, check: bool = True) -> str:
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
    if check:
        assert result.returncode == 0, result.stderr
    return result.stdout.strip()
