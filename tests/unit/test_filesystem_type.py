import subprocess
from pathlib import Path
from typing import Any

import memory_net.leaf.cli as cli


def test_assert_local_memory_net_home_aborts_on_nfs(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(cli, "_filesystem_type", lambda _path: "nfs")

    try:
        cli._assert_local_memory_net_home(tmp_path)
    except SystemExit as exc:
        assert "local filesystem" in str(exc)
    else:
        raise AssertionError("expected SystemExit")


def test_assert_local_memory_net_home_allows_unknown_fs(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(cli, "_filesystem_type", lambda _path: None)

    cli._assert_local_memory_net_home(tmp_path)


def test_filesystem_type_detects_darwin_smbfs(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    def fake_run(*args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert args[0][:3] == ["stat", "-f", "%T"]
        return subprocess.CompletedProcess(args[0], 0, stdout="smbfs\n", stderr="")

    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli._filesystem_type(tmp_path) == "smbfs"


def test_filesystem_type_uses_nearest_existing_ancestor(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    missing = tmp_path / "missing" / "state" / ".memory-net"

    def fake_run(*args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert args[0][-1] == str(tmp_path)
        return subprocess.CompletedProcess(args[0], 0, stdout="nfs\n", stderr="")

    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli._filesystem_type(missing) == "nfs"


def test_unknown_filesystem_policy_is_explicit() -> None:
    assert cli.UNKNOWN_FILESYSTEM_POLICY == "allow"
