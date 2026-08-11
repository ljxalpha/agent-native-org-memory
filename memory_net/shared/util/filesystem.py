"""Local-filesystem detection.

``MEMORY_NET_HOME`` holds a git worktree, lock files and a config generation
cache. On a network or shared filesystem the locking primitives this project
relies on are not reliable, so putting state there produces corruption that
looks like random failure. Detecting it up front turns that into one clear
refusal.

This lives in ``shared`` rather than in the leaf CLI because the installer's
preflight probe must reach the same verdict as the runtime does -- a check the
installer passes and the daemon then fails is worse than no check at all.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

#: Filesystem types that are definitely not local storage.
NON_LOCAL_FILESYSTEM_TYPES = frozenset(
    {"nfs", "nfs4", "smb", "smb2", "smb3", "smbfs", "cifs", "fuse.sshfs"}
)
#: What to do when the type cannot be determined at all (unsupported platform,
#: ``stat`` unavailable). ``allow`` keeps the project installable on platforms
#: this detection does not cover yet; the value is explicit so that flipping it
#: is a deliberate decision rather than an accident of control flow.
UNKNOWN_FILESYSTEM_POLICY = "allow"


def filesystem_type(path: Path) -> str | None:
    """Return the filesystem type of ``path``, or ``None`` when unknown.

    Resolves to the nearest existing ancestor so the answer is available before
    the directory itself has been created -- which is exactly when an installer
    wants it.
    """
    target = nearest_existing_ancestor(path)
    if not target.exists():
        return None
    if sys.platform.startswith("linux"):
        return _stat_filesystem_type(["stat", "-f", "-c", "%T", str(target)])
    if sys.platform == "darwin":
        return _stat_filesystem_type(["stat", "-f", "%T", str(target)])
    return None


def nearest_existing_ancestor(path: Path) -> Path:
    target = path
    while not target.exists() and target != target.parent:
        target = target.parent
    return target


def local_filesystem_refusal(path: Path, *, label: str) -> str | None:
    """Return a refusal message when ``path`` is not on local storage.

    Returns ``None`` when the path is acceptable, so callers choose their own
    failure mode (``SystemExit`` at runtime, a probe failure at install time)
    without this module deciding it for them.
    """
    fs_type = filesystem_type(path)
    if fs_type is None:
        if UNKNOWN_FILESYSTEM_POLICY == "deny":
            return f"{label} filesystem type is unknown: {path}"
        return None
    if fs_type in NON_LOCAL_FILESYSTEM_TYPES:
        return f"{label} must be on a local filesystem; got {fs_type}: {path}"
    return None


def _stat_filesystem_type(args: list[str]) -> str | None:
    result = subprocess.run(
        args,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().lower()
    return None
