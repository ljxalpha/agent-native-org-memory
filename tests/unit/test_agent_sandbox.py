"""The agent sandbox launcher (§5.7), exercised entirely without bwrap.

Every test here is a dry run or a pure function. What is being checked is the
plan and the credential path, and both are decidable without entering a
namespace -- which is also what makes them checkable on a machine that has no
bubblewrap at all.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = REPO_ROOT / "scripts" / "agent_sandbox"
RUNBOOK = REPO_ROOT / "docs" / "runbook.md"

RUNTIME_ROOT = Path("/opt/agent-runtime")
RESOLV_TARGET = Path("/mnt/wsl/resolv.conf")

SECRET = "not-a-real-key-0123456789"


def _load() -> ModuleType:
    # Loader named explicitly: the launcher is an extensionless executable, so
    # the import machinery cannot infer one from its name.
    spec = importlib.util.spec_from_file_location(
        "agent_sandbox",
        LAUNCHER,
        loader=importlib.machinery.SourceFileLoader("agent_sandbox", str(LAUNCHER)),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sandbox = _load()
SOURCE = LAUNCHER.read_text(encoding="utf-8")


def _plan(**kwargs: object) -> list[str]:
    return sandbox.mount_plan(
        sandbox=Path("/run/sbx"),
        runtime_root=RUNTIME_ROOT,
        resolv_target=RESOLV_TARGET,
        **kwargs,
    )


# -- (a) and (b): what the plan must never contain -----------------------


@pytest.mark.parametrize("name", [".hermes", ".codex", ".claude", ".openclaw"])
def test_the_plan_binds_no_real_agent_directory(name: str) -> None:
    """Absent, not read-only. A path that does not exist cannot be read, and
    cannot be written and then restored."""
    assert name not in " ".join(_plan())


def test_a_root_inside_a_real_agent_directory_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".hermes" / "profiles").mkdir(parents=True)

    with pytest.raises(SystemExit, match="refusing a sandbox root"):
        sandbox.assert_root_is_safe(
            home / ".hermes" / "profiles" / "sbx", environ={"HOME": str(home)}
        )


def test_a_root_inside_this_repository_is_refused() -> None:
    """`clean` is `rm -rf` on this path."""
    with pytest.raises(SystemExit, match="inside the repository"):
        sandbox.assert_root_is_safe(REPO_ROOT / "build" / "sbx", environ={"HOME": "/x"})


def test_a_root_reached_through_a_symlink_is_still_refused(tmp_path: Path) -> None:
    """The interesting way to arrive at a forbidden path."""
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    link = tmp_path / "innocent"
    link.symlink_to(home / ".codex")

    with pytest.raises(SystemExit, match="refusing a sandbox root"):
        sandbox.assert_root_is_safe(link / "sbx", environ={"HOME": str(home)})


# -- (c): the two allowlists ---------------------------------------------


def test_the_two_allowlists_are_defined_separately() -> None:
    """They answer different questions -- what this process reads, and what the
    agent under test can see -- and merging them widens the second silently."""
    assert "HOME" in sandbox.LAUNCHER_ENV_ALLOWLIST
    assert set(sandbox.LAUNCHER_ENV_ALLOWLIST) != set(sandbox.SANDBOX_ENV_ALLOWLIST)
    assert "XDG_RUNTIME_DIR" not in sandbox.SANDBOX_ENV_ALLOWLIST
    assert "AOMT_SANDBOX_ROOT" not in sandbox.SANDBOX_ENV_ALLOWLIST


def test_the_launcher_reads_only_its_allowlist() -> None:
    kept = sandbox.launcher_env(
        {"HOME": "/h", "SLACK_TOKEN": SECRET, "AWS_SECRET_ACCESS_KEY": SECRET}
    )

    assert kept == {"HOME": "/h"}


def test_the_sandbox_environment_uses_neutral_paths_not_the_host_home() -> None:
    env = sandbox.sandbox_env(runtime_root=RUNTIME_ROOT, credentials={})

    assert env["HOME"] == "/home/agent"
    assert env["HERMES_HOME"] == "/home/agent/.hermes"
    assert set(env) <= set(sandbox.SANDBOX_ENV_ALLOWLIST)


def test_doctor_reports_the_credential_source_and_never_the_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / "creds.env"
    env_file.write_text(
        f"DEEPSEEK_BASE_URL=https://endpoint.example.invalid/v1\nSLACK_TOKEN={SECRET}\n",
        encoding="utf-8",
    )

    report = sandbox.credential_report(
        env_file=env_file, environ={"DEEPSEEK_API_KEY": SECRET}
    )

    assert report[0] == ("DEEPSEEK_API_KEY", "env", "<set>")
    assert report[1] == ("DEEPSEEK_BASE_URL", str(env_file), "<set>")
    assert SECRET not in str(report)


def test_a_missing_credential_reads_as_missing_rather_than_as_empty(
    tmp_path: Path,
) -> None:
    report = sandbox.credential_report(env_file=tmp_path / "absent.env", environ={})

    assert [item[2] for item in report] == ["<missing>", "<missing>"]


# -- (d): the shapes that would put a credential somewhere readable ------


@pytest.mark.parametrize(
    "forbidden", ["eval(", "os.system", "shell=True", "env -i ", "--setenv"]
)
def test_the_source_contains_no_way_to_put_a_value_in_a_command_line(
    forbidden: str,
) -> None:
    """`env -i KEY=VALUE` and `--setenv KEY VALUE` both write the value into
    argv, which any process on the machine reads through `/proc`. A shell is
    excluded for the same reason -- a value on a command line is a value in a
    shell history."""
    assert forbidden not in SOURCE


def test_the_subcommands_are_exactly_the_four_that_exist() -> None:
    """No `env` subcommand: its whole purpose would be printing a value.

    Asserted against the parser rather than the file's text, so that adding one
    fails here whatever it is spelled like.
    """
    parser = sandbox.build_parser()
    actions = [
        action
        for action in parser._subparsers._group_actions  # type: ignore[union-attr]
    ]
    assert sorted(actions[0].choices) == ["clean", "doctor", "run", "up"]


def test_the_credential_reader_is_imported_rather_than_reimplemented() -> None:
    """One parser, one place deciding which keys are in scope."""
    assert "from memory_net.shared.env_allowlist import" in SOURCE
    assert "def parse_env_file" not in SOURCE


def test_no_credential_value_reaches_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _fake_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
        captured["argv"] = argv
        captured["env"] = env

    monkeypatch.setattr(sandbox.os, "execve", _fake_execve, raising=False)
    monkeypatch.setattr("memory_net.shared.env_allowlist.os.execve", _fake_execve)
    monkeypatch.setattr(
        "memory_net.shared.env_allowlist.shutil.which", lambda *a, **k: "/usr/bin/bwrap"
    )

    sandbox.do_run(
        Path("/tmp/aomt-sandbox-test"),
        runtime_root=RUNTIME_ROOT,
        command=["hermes", "--version"],
        read_only_agentroot=False,
        allow_proc=False,
        dry_run=False,
        env_file=None,
        environ={
            "HOME": "/nonexistent-home",
            "DEEPSEEK_API_KEY": SECRET,
            "DEEPSEEK_BASE_URL": "https://endpoint.example.invalid/v1",
        },
    )

    assert SECRET not in " ".join(captured["argv"])  # type: ignore[arg-type]
    assert captured["env"]["DEEPSEEK_API_KEY"] == SECRET  # type: ignore[index]


# -- (h) and (i): the two switches ---------------------------------------


def test_proc_is_absent_unless_it_is_asked_for() -> None:
    assert "--proc" not in _plan()
    assert "--proc" in _plan(allow_proc=True)


def test_read_only_agentroot_changes_the_bind_and_nothing_else() -> None:
    writable = _plan()
    readonly = _plan(read_only_agentroot=True)

    assert writable[writable.index("/work/agentroot") - 2] == "--bind"
    assert readonly[readonly.index("/work/agentroot") - 2] == "--ro-bind"
    assert len(writable) == len(readonly)


# -- (e), (f), (j): idempotence, literals, credential precedence ---------


def test_clean_is_idempotent_on_a_root_that_is_not_there(tmp_path: Path) -> None:
    assert sandbox.do_clean(tmp_path / "absent", environ={"HOME": str(tmp_path)}) == 0


def test_up_rewrites_the_config_and_leaves_the_sessions_alone(tmp_path: Path) -> None:
    """A previous run's traces are the evidence, not clutter."""
    root = tmp_path / "sbx"
    sandbox.do_up(root, toolset="none", environ={"HOME": str(tmp_path)})
    sessions = root / "home" / ".hermes" / "sessions"
    sessions.mkdir()
    (sessions / "old.jsonl").write_text("{}\n", encoding="utf-8")

    sandbox.do_up(root, toolset="file", environ={"HOME": str(tmp_path)})

    assert (sessions / "old.jsonl").exists()
    assert 'cli: ["file"]' in (root / "home" / ".hermes" / "config.yaml").read_text()


def test_the_default_toolset_is_no_tools_at_all() -> None:
    assert sandbox.TOOLSETS["none"] == "[]"
    assert "cli: []" in sandbox.config_yaml("none")


def test_the_source_holds_no_absolute_home_path_literal() -> None:
    """The plan's host paths come from variables. A literal here would be one
    machine's account name, committed."""
    assert re.search(r"/home/[a-z]", SOURCE.replace("/home/agent", "")) is None
    assert re.search(r"/Users/[A-Za-z]", SOURCE) is None


def test_the_process_environment_wins_over_the_credential_file(
    tmp_path: Path,
) -> None:
    """The channel an operator injects a separate, rate-limited key through:
    it must not be quietly overridden by the shared file."""
    report = sandbox.credential_report(
        env_file=tmp_path / "absent.env",
        environ={"DEEPSEEK_API_KEY": SECRET, "DEEPSEEK_BASE_URL": "https://x.invalid"},
    )

    assert [item[1] for item in report] == ["env", "env"]


# -- (g): the manifest in the runbook is the manifest in the code --------


def test_the_runbook_manifest_matches_the_assembled_plan() -> None:
    """Documentation as a test fixture.

    A mount manifest that is described in one place and built in another drifts
    silently, and the drift is only discovered by a sandbox that does not
    isolate what the document says it does.
    """
    documented = _runbook_manifest()
    assembled = _plan()

    assert documented == assembled


def _runbook_manifest() -> list[str]:
    text = RUNBOOK.read_text(encoding="utf-8")
    section = text.split("## Agent Sandbox Mount Manifest", 1)[1]
    block = section.split("```sh", 1)[1].split("```", 1)[0]
    flattened = block.replace("\\\n", " ")
    substituted = (
        flattened.replace("${RESOLV_TARGET}", str(RESOLV_TARGET))
        .replace("${AGENT_RUNTIME_ROOT}", str(RUNTIME_ROOT))
        .replace("${SANDBOX}", "/run/sbx")
    )
    items = substituted.split()
    # The document writes the command name; the plan carries its absolute path.
    assert items[0] == "bwrap"
    return [sandbox.BWRAP, *items[1:]]


# -- the two caller-supplied paths the plan is assembled from -------------


def test_a_runtime_root_that_carries_an_agent_directory_is_refused(
    tmp_path: Path,
) -> None:
    """The mount plan omits `~/.hermes` and the others; `--runtime-root ~` puts
    all of them back, and the plan still reads as correct."""
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)

    with pytest.raises(SystemExit, match="must be absent from the namespace"):
        sandbox.assert_runtime_root_is_safe(home, environ={"HOME": str(home)})


def test_a_runtime_root_inside_an_agent_directory_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".codex" / "src").mkdir(parents=True)

    with pytest.raises(SystemExit, match="must be absent from the namespace"):
        sandbox.assert_runtime_root_is_safe(
            home / ".codex" / "src", environ={"HOME": str(home)}
        )


def test_the_home_directory_itself_is_refused_as_a_runtime_root(
    tmp_path: Path,
) -> None:
    """Refused even on a machine with no agent installed, where the check above
    finds nothing to match."""
    home = tmp_path / "home"
    home.mkdir()

    with pytest.raises(SystemExit, match="carries every agent state directory"):
        sandbox.assert_runtime_root_is_safe(home, environ={"HOME": str(home)})


def test_an_ordinary_runtime_root_is_accepted(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    runtime = tmp_path / "agent-runtime"
    runtime.mkdir()

    sandbox.assert_runtime_root_is_safe(runtime, environ={"HOME": str(home)})


@pytest.mark.parametrize("name", ["home", "agentroot"])
def test_a_symlinked_bind_source_is_refused(tmp_path: Path, name: str) -> None:
    """Resolving the sandbox root does not cover its children, and bwrap mounts
    what a source symlink points at."""
    root = tmp_path / "sbx"
    root.mkdir()
    (tmp_path / "hermes-state").mkdir()
    (root / name).symlink_to(tmp_path / "hermes-state")

    with pytest.raises(SystemExit, match="it is a symlink"):
        sandbox.assert_mount_sources_are_not_symlinks(root)


def test_real_directories_under_the_root_are_accepted(tmp_path: Path) -> None:
    root = tmp_path / "sbx"
    (root / "home").mkdir(parents=True)
    (root / "agentroot").mkdir()

    sandbox.assert_mount_sources_are_not_symlinks(root)


def test_doctor_refuses_a_symlinked_bind_source(tmp_path: Path) -> None:
    """The failure mode a subset of `run`'s checks produces: a reassuring
    manifest printed for a configuration `run` would reject."""
    root = tmp_path / "sbx"
    root.mkdir()
    (root / "agentroot").symlink_to(tmp_path / "elsewhere")

    with pytest.raises(SystemExit, match="it is a symlink"):
        sandbox.do_doctor(
            root,
            runtime_root=RUNTIME_ROOT,
            env_file=None,
            since=None,
            environ={"HOME": str(tmp_path / "home")},
        )


def test_up_refuses_a_symlinked_home_before_it_writes(tmp_path: Path) -> None:
    """`mkdir(exist_ok=True)` follows an existing symlink, so `<root>/home`
    pointing at the real home would put this function's config.yaml into the
    operator's actual agent configuration. A write, not just an exposure."""
    root = tmp_path / "sbx"
    root.mkdir()
    real_home = tmp_path / "real-home"
    (real_home / ".hermes").mkdir(parents=True)
    (root / "home").symlink_to(real_home)

    with pytest.raises(SystemExit, match="it is a symlink"):
        sandbox.do_up(root, toolset="none", environ={"HOME": str(tmp_path / "home")})

    assert not (real_home / ".hermes" / "config.yaml").exists()
