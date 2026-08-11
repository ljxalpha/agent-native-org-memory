"""The pull request command: what it is handed, and what it is not.

A fake ``gh`` records its argv, its cwd and its environment. The environment
assertion is the one that matters: this process holds an LLM API key, and a PR
command that inherited it would put a credential in the hands of an arbitrary
operator-configured executable.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any

import pytest

from memory_net.distiller.cli import _build_parser, _open_pull_request
from memory_net.distiller.publisher import (
    DEFAULT_PR_COMMAND,
    PR_COMMAND_ENV,
    PR_ENV_ALLOWLIST_ENV,
    PullRequestError,
    build_review_request,
    open_pull_request,
    pr_environment,
)

RUN_ID = "20260811T101010Z-abcd1234"
SECRET_VALUE = "sk-test-0123456789abcdef"


def _request_file(tmp_path: Path) -> Path:
    payload = build_review_request(
        run_id=RUN_ID,
        head=f"release/{RUN_ID}",
        base="master",
        codeowners_path=".github/CODEOWNERS",
        master_snapshot_sha="a" * 40,
        products_count=1,
        categories=["common_knowledge"],
    )
    path = tmp_path / "review_request.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _stub(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "fake-gh"
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _environ(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "AOMT_DEEPSEEK_API_KEY": SECRET_VALUE,
        "ANTHROPIC_API_KEY": SECRET_VALUE,
    }
    env.update(extra)
    return env


def test_the_command_is_handed_the_artifacts_own_fields(tmp_path: Path) -> None:
    record = tmp_path / "argv.json"
    stub = _stub(
        tmp_path,
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"json.dump({{'argv': sys.argv[1:], 'cwd': os.getcwd(), "
        f"'env': dict(os.environ)}}, open({str(record)!r}, 'w'))\n"
        "print('https://example.invalid/pr/1')\n",
    )
    request = _request_file(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()

    url = open_pull_request(
        request,
        cwd=workdir,
        command=f"{stub} pr create",
        environ=_environ(tmp_path),
    )

    seen = json.loads(record.read_text(encoding="utf-8"))
    assert url == "https://example.invalid/pr/1"
    assert seen["argv"] == [
        "pr",
        "create",
        "--head",
        f"release/{RUN_ID}",
        "--base",
        "master",
        "--title",
        f"distill run {RUN_ID}",
        "--body-file",
        str(request),
    ]
    assert Path(seen["cwd"]).resolve() == workdir.resolve()
    # No credential value reaches the child, whatever it is named.
    env_text = json.dumps(seen["env"])
    assert not re.search(re.escape(SECRET_VALUE), env_text)
    assert not re.search(r"(?i)(api_key|token|secret)\"\s*:\s*\"\S", env_text)


def test_only_an_allowlisted_variable_is_forwarded(tmp_path: Path) -> None:
    environ = _environ(
        tmp_path,
        GH_HOST="github.example.invalid",
        **{PR_ENV_ALLOWLIST_ENV: "GH_HOST"},
    )

    env = pr_environment(environ)

    assert env["GH_HOST"] == "github.example.invalid"
    assert "AOMT_DEEPSEEK_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_a_failing_command_reports_both_streams(tmp_path: Path) -> None:
    stub = _stub(
        tmp_path,
        "#!/bin/sh\necho 'partial output'\necho 'not authenticated' >&2\nexit 4\n",
    )

    with pytest.raises(PullRequestError) as excinfo:
        open_pull_request(
            _request_file(tmp_path),
            cwd=tmp_path,
            command=str(stub),
            environ=_environ(tmp_path),
        )

    assert "exit code 4" in str(excinfo.value)
    assert "partial output" in str(excinfo.value)
    assert "not authenticated" in str(excinfo.value)


def test_a_command_that_prints_no_url_is_a_failure(tmp_path: Path) -> None:
    stub = _stub(tmp_path, "#!/bin/sh\necho 'created something'\n")

    with pytest.raises(PullRequestError, match="printed no URL"):
        open_pull_request(
            _request_file(tmp_path),
            cwd=tmp_path,
            command=str(stub),
            environ=_environ(tmp_path),
        )


def test_a_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "grandchild.pid"
    stub = _stub(
        tmp_path,
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(60)\n",
    )

    with pytest.raises(PullRequestError, match="timed out"):
        open_pull_request(
            _request_file(tmp_path),
            cwd=tmp_path,
            command=str(stub),
            timeout_s=1.0,
            environ=_environ(tmp_path),
        )

    pid = int(marker.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and _alive(pid):
        time.sleep(0.05)
    assert not _alive(pid)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def test_the_flag_is_off_by_default_and_runs_no_command(tmp_path: Path) -> None:
    calls = tmp_path / "called"
    stub = _stub(tmp_path, f"#!/bin/sh\ntouch {calls}\n")
    args = _build_parser().parse_args(["run", "--repo", str(tmp_path)])
    os.environ[PR_COMMAND_ENV] = str(stub)
    try:
        assert args.open_pr is False
        assert _open_pull_request(args, run_id=RUN_ID) is None
    finally:
        os.environ.pop(PR_COMMAND_ENV, None)

    assert not calls.exists()


def test_the_default_command_is_gh_pr_create() -> None:
    assert DEFAULT_PR_COMMAND == "gh pr create"


def test_asking_for_a_pr_without_a_review_request_stops_the_run(
    tmp_path: Path,
) -> None:
    args = argparse.Namespace(open_pr=True, dry_run=False, no_push=False, repo=tmp_path)

    with pytest.raises(SystemExit, match="requires a review request"):
        _open_pull_request(args, run_id=RUN_ID)


@pytest.mark.parametrize("flag", ["dry_run", "no_push"])
def test_a_pr_is_refused_when_nothing_was_pushed(tmp_path: Path, flag: str) -> None:
    args = argparse.Namespace(open_pr=True, dry_run=False, no_push=False, repo=tmp_path)
    setattr(args, flag, True)

    with pytest.raises(SystemExit, match="needs a pushed release branch"):
        _open_pull_request(args, run_id=RUN_ID)


def test_the_body_comes_from_the_artifact_and_is_not_reassembled(
    tmp_path: Path,
) -> None:
    """The ``--body-file`` argument is the artifact itself, byte for byte."""
    record = tmp_path / "body.txt"
    stub = _stub(
        tmp_path,
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "argv = sys.argv[1:]\n"
        "path = argv[argv.index('--body-file') + 1]\n"
        f"open({str(record)!r}, 'w').write(open(path).read())\n"
        "print('https://example.invalid/pr/2')\n",
    )
    request = _request_file(tmp_path)

    open_pull_request(
        request, cwd=tmp_path, command=str(stub), environ=_environ(tmp_path)
    )

    assert record.read_text(encoding="utf-8") == request.read_text(encoding="utf-8")


def test_a_tampered_artifact_never_reaches_the_command(tmp_path: Path) -> None:
    calls = tmp_path / "called"
    stub = _stub(tmp_path, f"#!/bin/sh\ntouch {calls}\necho https://example.invalid\n")
    request = _request_file(tmp_path)
    payload: dict[str, Any] = json.loads(request.read_text(encoding="utf-8"))
    payload["head"] = "release/$(id)"
    request.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(Exception, match="not a branch name"):
        open_pull_request(
            request, cwd=tmp_path, command=str(stub), environ=_environ(tmp_path)
        )

    assert not calls.exists()
