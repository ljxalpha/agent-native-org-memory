"""A tracked Python file has a shebang if and only if git records it executable.

This is the invariant behind ruff's ``EXE001`` (shebang present, file not
executable) and ``EXE002`` (file executable, no shebang). Both rules are in
ruff's default set, so CI enforces them -- but ruff deliberately skips them on
WSL, where the executable bit of a Windows filesystem cannot be trusted. On WSL
``ruff check .`` therefore passes on a tree that fails the lint step on a Linux
runner, and every later CI step is skipped with it.

The check below reads the file mode from the git index rather than from the
filesystem. The index is what a checkout reproduces on the runner, and reading
it gives the same verdict on every platform. The consequence is that a
``chmod +x`` counts only once it is staged (``git add`` or
``git update-index --chmod=+x``), which is also the point at which it would
reach CI.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

EXECUTABLE = "100755"
REGULAR = "100644"
SHEBANG = b"#!"


def _git(repo: Path, *args: str) -> str:
    """Run ``git`` in ``repo`` and return stdout; raise on a non-zero exit."""
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout


def _python_index_entries(repo: Path) -> list[tuple[str, str]]:
    """Return ``(mode, path)`` for every ``*.py`` file staged in ``repo``'s index.

    Symlinks (``120000``) and submodules (``160000``) are excluded: they carry no
    executable bit of their own.
    """
    entries: list[tuple[str, str]] = []
    for record in _git(repo, "ls-files", "-s", "-z", "--", "*.py").split("\0"):
        if not record:
            continue
        meta, path = record.split("\t", 1)
        mode = meta.split(" ", 1)[0]
        if mode in (EXECUTABLE, REGULAR):
            entries.append((mode, path))
    return entries


def find_mismatches(repo: Path) -> tuple[list[str], list[str], int]:
    """Compare the shebang of each staged Python file against its index mode.

    Returns ``(shebang_not_executable, executable_without_shebang, scanned)``.
    The first list mirrors ruff ``EXE001``, the second ``EXE002``; ``scanned`` is
    the number of files examined, so a caller can tell "clean" from "saw nothing".
    """
    not_executable: list[str] = []
    no_shebang: list[str] = []
    entries = _python_index_entries(repo)
    for mode, path in entries:
        with (repo / path).open("rb") as handle:
            has_shebang = handle.read(len(SHEBANG)) == SHEBANG
        if has_shebang and mode != EXECUTABLE:
            not_executable.append(path)
        elif not has_shebang and mode == EXECUTABLE:
            no_shebang.append(path)
    return sorted(not_executable), sorted(no_shebang), len(entries)


def _require_git_checkout() -> None:
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout (e.g. an sdist): no index to read modes from")


def test_shebang_python_files_are_executable_in_the_index() -> None:
    _require_git_checkout()
    not_executable, _, scanned = find_mismatches(REPO_ROOT)
    assert scanned > 0, "no tracked *.py files found; the guard would pass vacuously"
    assert not_executable == [], (
        "these files start with '#!' but git records them as non-executable "
        "(ruff EXE001 on CI); fix with `git update-index --chmod=+x <path>`: "
        f"{not_executable}"
    )


def test_executable_python_files_have_a_shebang() -> None:
    _require_git_checkout()
    _, no_shebang, scanned = find_mismatches(REPO_ROOT)
    assert scanned > 0, "no tracked *.py files found; the guard would pass vacuously"
    assert no_shebang == [], (
        "these files are executable in git but have no '#!' line "
        "(ruff EXE002 on CI); add a shebang or `git update-index --chmod=-x <path>`: "
        f"{no_shebang}"
    )


def test_find_mismatches_flags_both_directions_on_a_synthetic_repo(
    tmp_path: Path,
) -> None:
    """The checker itself must catch each kind of mismatch and nothing else."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    files = {
        "ok_script.py": "#!/usr/bin/env python3\nprint('ok')\n",
        "ok_module.py": "VALUE = 1\n",
        "bad_shebang.py": "#!/usr/bin/env python3\nprint('bad')\n",
        "bad_exec.py": "VALUE = 2\n",
        "nested/deep_script.py": "#!/usr/bin/env python3\n",
        "notes.txt": "#!/not/python\n",
    }
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, "add", ".")
    # Set modes in the index explicitly so the fixture does not depend on the
    # host filesystem honouring chmod.
    _git(repo, "update-index", "--chmod=-x", *files)
    _git(repo, "update-index", "--chmod=+x", "ok_script.py", "bad_exec.py")

    not_executable, no_shebang, scanned = find_mismatches(repo)

    # notes.txt is not a Python file and must not be scanned.
    assert scanned == 5
    assert not_executable == ["bad_shebang.py", "nested/deep_script.py"]
    assert no_shebang == ["bad_exec.py"]

    # Staging the fix is what clears the finding.
    _git(repo, "update-index", "--chmod=+x", "bad_shebang.py", "nested/deep_script.py")
    _git(repo, "update-index", "--chmod=-x", "bad_exec.py")
    assert find_mismatches(repo) == ([], [], 5)
