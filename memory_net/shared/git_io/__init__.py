from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitResult:
    args: list[str]
    cwd: Path
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class GitBytesResult:
    args: list[str]
    cwd: Path
    returncode: int
    stdout: bytes
    stderr: bytes


class GitRepo:
    def __init__(
        self,
        path: Path,
        *,
        dry_run: bool = False,
        no_push: bool = False,
    ) -> None:
        self.path = path
        self.dry_run = dry_run
        self.no_push = no_push

    @classmethod
    def clone(
        cls,
        remote_url: str,
        path: Path,
        *,
        dry_run: bool = False,
        no_push: bool = False,
    ) -> "GitRepo":
        if not dry_run and not path.exists():
            _run(["git", "clone", remote_url, str(path)], Path.cwd())
        return cls(path, dry_run=dry_run, no_push=no_push)

    def run(self, *args: str, check: bool = True) -> GitResult:
        return _run(["git", *args], self.path, check=check)

    def succeeded(self, *args: str) -> bool:
        return self.run(*args, check=False).returncode == 0

    def fetch(self) -> None:
        if self.dry_run:
            return
        self.run("fetch", "--all", "--prune")

    def switch(self, ref: str, *, create: bool = False, orphan: bool = False) -> None:
        if self.dry_run:
            return
        if orphan:
            self.run("switch", "--orphan", ref)
        elif create:
            self.run("switch", "-C", ref)
        else:
            self.run("switch", ref)

    def current_sha(self, ref: str = "HEAD") -> str:
        return self.run("rev-parse", ref).stdout.strip()

    def show_text(self, ref_path: str) -> str:
        return self.run("show", ref_path).stdout

    def show_bytes(self, ref_path: str) -> bytes:
        return _run_bytes(["git", "show", ref_path], self.path).stdout

    def show_json(self, ref_path: str) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.show_text(ref_path)))

    def add_commit(
        self,
        paths: list[str],
        message: str,
        *,
        allow_empty: bool = False,
    ) -> bool:
        if self.dry_run:
            return False
        if paths:
            self.run("add", *paths)
        elif not allow_empty:
            return False
        diff = self.run("diff", "--cached", "--quiet", check=False)
        if diff.returncode == 0 and not allow_empty:
            return False
        args = ["commit"]
        if allow_empty:
            args.append("--allow-empty")
        args.extend(["-m", message])
        self.run(*args)
        return True

    def push(self, refspec: str | None = None) -> None:
        if self.dry_run or self.no_push:
            return
        if refspec:
            self.run("push", "origin", refspec)
        else:
            self.run("push")


def _run(args: list[str], cwd: Path, *, check: bool = True) -> GitResult:
    env = os.environ.copy()
    env.update(
        {
            "LC_ALL": "C.UTF-8",
            "LANG": "C.UTF-8",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    proc = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and proc.returncode != 0:
        raise GitError(f"{' '.join(args)} failed: {proc.stderr.strip()}")
    return GitResult(
        args=args,
        cwd=cwd,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def _run_bytes(args: list[str], cwd: Path, *, check: bool = True) -> GitBytesResult:
    env = os.environ.copy()
    env.update(
        {
            "LC_ALL": "C.UTF-8",
            "LANG": "C.UTF-8",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    proc = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and proc.returncode != 0:
        message = proc.stderr.decode("utf-8", errors="replace").strip()
        raise GitError(f"{' '.join(args)} failed: {message}")
    return GitBytesResult(
        args=args,
        cwd=cwd,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )
