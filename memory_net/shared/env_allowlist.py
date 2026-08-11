"""Reading two credentials out of a shared ``.env``, and nothing else (§5.6).

The file this reads is a personal, shared-purpose credential store: in practice
it holds dozens of unrelated keys for unrelated services. Sourcing it, or
handing the whole thing to a subprocess, takes everything in it -- so the reader
here is deliberately not a shell:

* no expansion, no command substitution, no ``source`` and no ``export``;
* keys outside the caller's allowlist are dropped as they are read;
* values reach a child process only through the ``env`` argument of ``execve``,
  never through ``argv``, which any process on the machine can read out of
  ``/proc/<pid>/cmdline``;
* nothing this module prints ever contains a value, and there is deliberately no
  eval-able output mode -- an eval-able line is a value on a terminal, in a
  shell history and in any log that captured it.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

DEFAULT_ALLOWLIST = ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL")
DEFAULT_ENV_FILE = Path("~/.hermes/.env")

SET_MARKER = "<set>"
MISSING_MARKER = "<missing>"

#: ``KEY=value``, and nothing else. An ``export`` prefix, indentation-only
#: variants and shell constructs are not accepted forms: a reader that quietly
#: understood a *subset* of shell would be trusted to understand the rest.
_ASSIGNMENT = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$")


class CredentialError(RuntimeError):
    """The requested credentials could not be resolved.

    Carries key names and paths only. A message quoting the value it failed on
    would put the credential in whatever log caught the traceback.
    """


def parse_env_file(text: str, *, allowlist: Sequence[str]) -> dict[str, str]:
    """The allowlisted assignments in ``text``.

    A later assignment to the same key wins, matching what a shell would do for
    the one behaviour that is unsurprising. Everything else a shell would do --
    expansion, substitution, continuation -- is simply not implemented, so a
    value containing ``$(id)`` is that literal string.
    """
    wanted = set(allowlist)
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _ASSIGNMENT.match(line)
        if match is None:
            # Includes `export KEY=value`: rejected rather than accepted,
            # because accepting it claims a shell compatibility this does not
            # have.
            continue
        key = match.group("key")
        if key not in wanted:
            # Dropped here, not filtered later: an unrelated service's key must
            # not exist in this process's memory at all.
            continue
        values[key] = _unquote(match.group("value").strip())
    return values


def resolve(
    *,
    allowlist: Sequence[str] = DEFAULT_ALLOWLIST,
    env_file: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The allowlisted values, process environment first, file second.

    The process environment wins because it is what a caller set deliberately
    for this run; the file is the fallback for an interactive machine. Neither
    being present is an error rather than an empty result: continuing with no
    credentials produces an authentication failure several steps later, at a
    point that says nothing about the cause.
    """
    environ = os.environ if environ is None else environ
    resolved = {key: environ[key] for key in allowlist if environ.get(key)}
    missing = [key for key in allowlist if key not in resolved]
    if not missing:
        return resolved
    path = (env_file or DEFAULT_ENV_FILE).expanduser()
    if not path.is_file():
        raise CredentialError(
            f"missing credentials {', '.join(missing)}: not in the environment, "
            f"and no credential file at {path}"
        )
    # Read-only, always. This file is never written, moved or copied.
    from_file = parse_env_file(path.read_text(encoding="utf-8"), allowlist=missing)
    resolved.update({key: value for key, value in from_file.items() if value})
    still_missing = [key for key in allowlist if key not in resolved]
    if still_missing:
        raise CredentialError(
            f"missing credentials {', '.join(still_missing)}: not in the "
            f"environment and not in {path}"
        )
    return resolved


def exec_with_env(
    argv: Sequence[str],
    *,
    credentials: Mapping[str, str],
    environ: Mapping[str, str] | None = None,
) -> None:
    """Replace this process with ``argv``, credentials in its environment.

    The values travel in ``env`` and only there. ``env -i KEY=VALUE`` and
    ``--setenv KEY VALUE`` both put them in ``argv``, which is world-readable
    through ``/proc``; this call does neither.
    """
    if not argv:
        raise CredentialError("no command given after --")
    environ = os.environ if environ is None else environ
    executable = shutil.which(argv[0], path=environ.get("PATH"))
    if executable is None:
        raise CredentialError(f"command not found: {argv[0]}")
    os.execve(executable, list(argv), {**environ, **credentials})


def exec_in_sandbox(
    bwrap_argv: Sequence[str],
    *,
    credentials: Mapping[str, str],
    environ: Mapping[str, str] | None = None,
) -> None:
    """Same handoff, through ``bwrap``.

    bwrap passes its *own* environment to the process inside the sandbox, so
    neither ``env -i`` nor ``--setenv`` is needed to get the values in -- and
    both of those would write them into a command line.
    """
    exec_with_env(bwrap_argv, credentials=credentials, environ=environ)


def main(argv: Sequence[str] | None = None) -> int:
    """``aomt-with-env [--env-file P] [--key K] (--check | --print-keys | -- cmd)``.

    Three modes, and no fourth: there is no ``env`` subcommand and no eval-able
    output, because both exist only to move a value onto a terminal.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    command: list[str] = []
    if "--" in arguments:
        index = arguments.index("--")
        arguments, command = arguments[:index], arguments[index + 1 :]
    env_file: Path | None = None
    keys: list[str] = []
    mode = "exec"
    while arguments:
        item = arguments.pop(0)
        if item == "--check":
            mode = "check"
        elif item == "--print-keys":
            mode = "print-keys"
        elif item == "--env-file":
            if not arguments:
                raise CredentialError("--env-file needs a path")
            env_file = Path(arguments.pop(0))
        elif item == "--key":
            if not arguments:
                raise CredentialError("--key needs a name")
            keys.append(arguments.pop(0))
        else:
            raise CredentialError(f"unknown argument: {item}")
    allowlist = tuple(keys) or DEFAULT_ALLOWLIST

    if mode == "print-keys":
        # Names only: the sandbox launcher needs to know *which* variables to
        # carry, never what is in them.
        for key in allowlist:
            print(key)
        return 0
    if mode == "check":
        try:
            resolved = resolve(allowlist=allowlist, env_file=env_file)
        except CredentialError:
            resolved = {}
        for key in allowlist:
            print(f"{key}: {SET_MARKER if resolved.get(key) else MISSING_MARKER}")
        return 0 if len(resolved) == len(allowlist) else 1
    if not command:
        raise CredentialError(
            "nothing to do: pass --check, --print-keys, or -- followed by a command"
        )
    exec_with_env(command, credentials=resolve(allowlist=allowlist, env_file=env_file))
    return 0  # pragma: no cover - execve does not return


def _unquote(value: str) -> str:
    """Strip one matched pair of quotes; leave everything else alone."""
    for quote in ('"', "'"):
        if len(value) >= 2 and value.startswith(quote) and value.endswith(quote):
            return value[1:-1]
    return value
