"""The credential launcher (§5.6).

The value used throughout is a marker string, and most assertions are of the
form "this string does not appear here". That is the property under test: a
credential must reach the child process's environment and no other surface --
not a command line, not stdout, not an exception message.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from memory_net.shared import env_allowlist
from memory_net.shared.env_allowlist import (
    DEFAULT_ALLOWLIST,
    CredentialError,
    exec_with_env,
    main,
    parse_env_file,
    resolve,
)

SECRET = "sk-deepseek-NOTAREALKEY-0123456789"
BASE_URL = "https://api.example.invalid/v1"

ENV_TEXT = f"""
# a shared credential store, most of it unrelated
SLACK_BOT_TOKEN=xoxb-unrelated-token
DEEPSEEK_API_KEY={SECRET}
DEEPSEEK_BASE_URL="{BASE_URL}"
SEARCH_API_KEY='another-unrelated'
"""


def _env_file(tmp_path: Path, text: str = ENV_TEXT) -> Path:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def test_only_the_allowlisted_keys_are_parsed() -> None:
    values = parse_env_file(ENV_TEXT, allowlist=DEFAULT_ALLOWLIST)

    assert values == {"DEEPSEEK_API_KEY": SECRET, "DEEPSEEK_BASE_URL": BASE_URL}


def test_a_file_of_many_keys_yields_only_two() -> None:
    text = "\n".join(f"UNRELATED_KEY_{index}=value{index}" for index in range(30))
    text += f"\nDEEPSEEK_API_KEY={SECRET}\nDEEPSEEK_BASE_URL={BASE_URL}\n"

    values = parse_env_file(text, allowlist=DEFAULT_ALLOWLIST)

    assert sorted(values) == ["DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"]


@pytest.mark.parametrize(
    "line",
    [
        f"DEEPSEEK_API_KEY={SECRET}",
        f'DEEPSEEK_API_KEY="{SECRET}"',
        f"DEEPSEEK_API_KEY='{SECRET}'",
        f"  DEEPSEEK_API_KEY={SECRET}  ",
    ],
)
def test_quoting_and_padding_are_accepted(line: str) -> None:
    assert parse_env_file(line, allowlist=["DEEPSEEK_API_KEY"]) == {
        "DEEPSEEK_API_KEY": SECRET
    }


def test_comments_and_blank_lines_are_ignored() -> None:
    text = f"# DEEPSEEK_API_KEY=commented-out\n\nDEEPSEEK_API_KEY={SECRET}\n"

    assert parse_env_file(text, allowlist=["DEEPSEEK_API_KEY"]) == {
        "DEEPSEEK_API_KEY": SECRET
    }


def test_an_export_prefix_is_rejected_not_silently_accepted() -> None:
    """Accepting it would claim a shell compatibility this reader does not have,
    and the next construct someone writes would be silently misread."""
    assert (
        parse_env_file(
            f"export DEEPSEEK_API_KEY={SECRET}", allowlist=["DEEPSEEK_API_KEY"]
        )
        == {}
    )


def test_command_substitution_stays_a_literal() -> None:
    values = parse_env_file(
        "DEEPSEEK_API_KEY=$(cat /etc/shadow)", allowlist=["DEEPSEEK_API_KEY"]
    )

    assert values == {"DEEPSEEK_API_KEY": "$(cat /etc/shadow)"}


def test_variable_references_are_not_expanded() -> None:
    values = parse_env_file(
        "DEEPSEEK_BASE_URL=${OTHER}/v1", allowlist=["DEEPSEEK_BASE_URL"]
    )

    assert values == {"DEEPSEEK_BASE_URL": "${OTHER}/v1"}


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def test_the_process_environment_wins_over_the_file(tmp_path: Path) -> None:
    resolved = resolve(
        env_file=_env_file(tmp_path),
        environ={"DEEPSEEK_API_KEY": "from-environment"},
    )

    assert resolved["DEEPSEEK_API_KEY"] == "from-environment"
    assert resolved["DEEPSEEK_BASE_URL"] == BASE_URL


def test_a_missing_file_is_an_error_not_an_empty_environment(
    tmp_path: Path,
) -> None:
    """Continuing without credentials fails several steps later, at a point
    that says nothing about the cause."""
    with pytest.raises(CredentialError, match="no credential file"):
        resolve(env_file=tmp_path / "absent.env", environ={})


def test_the_default_path_is_reported_unexpanded(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The default location prints as `~/.hermes/.env`, never as the home it
    resolves to.

    The path is not a credential, but these messages reach CI logs and bug
    reports, and an expanded default names the machine's real user.
    """
    # Deliberately not shaped like `/home/<name>`: this file is public, and a
    # realistic-looking home path here would itself be the thing being guarded
    # against. `expanduser` reads $HOME whatever shape it has.
    monkeypatch.setenv("HOME", "/nonexistent-root/a-real-person")

    with pytest.raises(CredentialError) as excinfo:
        resolve(environ={})

    message = str(excinfo.value)
    assert "~/.hermes/.env" in message
    assert "a-real-person" not in message


def test_an_explicit_path_is_echoed_as_the_caller_gave_it(tmp_path: Path) -> None:
    """Echoing back `--env-file` discloses nothing the caller did not supply."""
    absent = tmp_path / "absent.env"

    with pytest.raises(CredentialError) as excinfo:
        resolve(env_file=absent, environ={})

    assert str(absent) in str(excinfo.value)


def test_a_file_missing_one_key_names_that_key(tmp_path: Path) -> None:
    path = _env_file(tmp_path, f"DEEPSEEK_API_KEY={SECRET}\n")

    with pytest.raises(CredentialError) as excinfo:
        resolve(env_file=path, environ={})

    assert "DEEPSEEK_BASE_URL" in str(excinfo.value)
    assert SECRET not in str(excinfo.value)


# --------------------------------------------------------------------------
# Handing over to the command
# --------------------------------------------------------------------------


def _capture_execve(monkeypatch: Any) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def fake_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
        seen.update(path=path, argv=argv, env=env)

    monkeypatch.setattr(env_allowlist.os, "execve", fake_execve)
    monkeypatch.setattr(
        env_allowlist.shutil, "which", lambda name, path=None: f"/usr/bin/{name}"
    )
    return seen


def test_the_value_reaches_env_and_never_argv(tmp_path: Path, monkeypatch: Any) -> None:
    """argv is readable by any process on the machine through /proc."""
    seen = _capture_execve(monkeypatch)

    exec_with_env(
        ["printenv", "DEEPSEEK_API_KEY"],
        credentials={"DEEPSEEK_API_KEY": SECRET},
        environ={"PATH": "/usr/bin"},
    )

    assert seen["env"]["DEEPSEEK_API_KEY"] == SECRET
    assert SECRET not in " ".join(seen["argv"])
    assert SECRET not in seen["path"]


def test_the_launcher_execs_the_command_after_the_separator(
    tmp_path: Path, monkeypatch: Any
) -> None:
    seen = _capture_execve(monkeypatch)
    monkeypatch.setattr(env_allowlist.os, "environ", {"PATH": "/usr/bin"})

    main(["--env-file", str(_env_file(tmp_path)), "--", "printenv", "DEEPSEEK_API_KEY"])

    assert seen["argv"] == ["printenv", "DEEPSEEK_API_KEY"]
    assert seen["env"]["DEEPSEEK_API_KEY"] == SECRET
    assert SECRET not in " ".join(seen["argv"])


def test_an_unknown_command_is_reported_by_name(monkeypatch: Any) -> None:
    monkeypatch.setattr(env_allowlist.shutil, "which", lambda name, path=None: None)

    with pytest.raises(CredentialError, match="command not found"):
        exec_with_env(
            ["definitely-not-installed"],
            credentials={"DEEPSEEK_API_KEY": SECRET},
            environ={"PATH": "/usr/bin"},
        )


def test_no_command_after_the_separator_is_an_error() -> None:
    with pytest.raises(CredentialError, match="no command"):
        exec_with_env([], credentials={})


# --------------------------------------------------------------------------
# The read-only modes print no values
# --------------------------------------------------------------------------


def test_check_reports_presence_only(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setattr(env_allowlist.os, "environ", {})

    code = main(["--env-file", str(_env_file(tmp_path)), "--check"])
    printed = capsys.readouterr().out

    assert code == 0
    assert printed.splitlines() == [
        "DEEPSEEK_API_KEY: <set>",
        "DEEPSEEK_BASE_URL: <set>",
    ]
    assert SECRET not in printed
    assert BASE_URL not in printed


def test_check_reports_a_missing_key_without_failing_hard(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setattr(env_allowlist.os, "environ", {})
    path = _env_file(tmp_path, f"DEEPSEEK_API_KEY={SECRET}\n")

    code = main(["--env-file", str(path), "--check"])
    printed = capsys.readouterr().out

    assert code == 1
    assert "DEEPSEEK_BASE_URL: <missing>" in printed
    assert SECRET not in printed


def test_print_keys_prints_names_only(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setattr(env_allowlist.os, "environ", {"DEEPSEEK_API_KEY": SECRET})

    main(["--print-keys"])
    printed = capsys.readouterr().out

    assert printed.splitlines() == list(DEFAULT_ALLOWLIST)
    assert SECRET not in printed


def test_there_is_no_eval_able_output_mode() -> None:
    """The mode that would exist to be `eval`-ed is the one that puts a value on
    a terminal and into a shell history."""
    for rejected in ("--env", "env", "--export", "--shell"):
        with pytest.raises(CredentialError, match="unknown argument"):
            main([rejected])


def test_doing_nothing_is_refused() -> None:
    with pytest.raises(CredentialError, match="nothing to do"):
        main([])
