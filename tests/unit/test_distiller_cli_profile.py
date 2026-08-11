"""Profile selection for a distillation run (§5.5.5).

The property that matters is not "a flag exists" but *where the bytes come
from*: a selected profile has to be read out of the frozen snapshot like every
other run input. Loading one from an arbitrary filesystem path would leave the
run's recorded ``master_snapshot_sha`` looking clean while the configuration it
actually ran under lived outside the snapshot entirely.
"""

import subprocess
from pathlib import Path
from typing import Any

import pytest

from memory_net.distiller import cli as cli_module

DEFAULT_PROFILE = "config/distill.yaml"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "HOME": str(repo),
            "PATH": "/usr/bin:/bin",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    )


def _repo_with_profiles(tmp_path: Path) -> tuple[Path, str]:
    """A committed repo holding two profiles, plus its snapshot sha."""
    repo = tmp_path / "repo"
    (repo / "config" / "profiles").mkdir(parents=True)
    (repo / "config" / "distill.yaml").write_text(
        "llm:\n  provider: mock\n  budget_usd: 5.0\n", encoding="utf-8"
    )
    (repo / "config" / "profiles" / "production.yaml").write_text(
        "llm:\n  provider: deepseek\n  budget_usd: 2.0\n", encoding="utf-8"
    )
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "profiles")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return repo, sha


# --------------------------------------------------------------------------
# Selection precedence
# --------------------------------------------------------------------------


def test_the_default_profile_is_unchanged(monkeypatch: Any) -> None:
    """Every existing run must keep loading exactly the file it loads today."""
    monkeypatch.delenv("AOMT_DISTILL_PROFILE", raising=False)

    selected = cli_module._selected_profile(_args())

    assert selected.path == DEFAULT_PROFILE
    assert selected.explicit is False


def test_the_environment_variable_selects_a_profile(monkeypatch: Any) -> None:
    monkeypatch.setenv("AOMT_DISTILL_PROFILE", "config/profiles/production.yaml")

    selected = cli_module._selected_profile(_args())

    assert selected.path == "config/profiles/production.yaml"
    assert selected.explicit is True


def test_the_flag_overrides_the_environment_variable(monkeypatch: Any) -> None:
    """The flag is typed for this one run; the variable is ambient. Ambient
    state silently winning is how a run ends up on the wrong budget."""
    monkeypatch.setenv("AOMT_DISTILL_PROFILE", "config/profiles/production.yaml")

    selected = cli_module._selected_profile(_args(config="config/distill.yaml"))

    assert selected.path == DEFAULT_PROFILE
    assert selected.explicit is True


# --------------------------------------------------------------------------
# The path may not leave the repo
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "/etc/passwd",
        "../outside/distill.yaml",
        "config/../../distill.yaml",
        "config/..",
        "",
    ],
)
def test_a_path_that_could_leave_the_repo_is_refused(raw: str) -> None:
    """Reading a profile from outside the snapshot would leave the recorded
    master_snapshot_sha looking clean while the run used configuration the
    snapshot never contained.

    The empty string is in here on purpose: falling through to the default
    would run a profile the caller never named, and they asked for something.
    """
    with pytest.raises(SystemExit, match="profile path"):
        cli_module._selected_profile(_args(config=raw))


def test_a_redundant_current_directory_segment_is_normalised_not_refused() -> None:
    """``.`` cannot escape anything, so refusing it would only be pedantry."""
    assert (
        cli_module._selected_profile(_args(config="config/./distill.yaml")).path
        == DEFAULT_PROFILE
    )


def test_a_backslash_path_is_normalised_before_it_is_checked() -> None:
    """Otherwise a Windows-style separator smuggles a .. segment past a naive
    split on '/'."""
    with pytest.raises(SystemExit, match="profile path"):
        cli_module._selected_profile(_args(config="config\\..\\..\\distill.yaml"))


# --------------------------------------------------------------------------
# The bytes come from the frozen snapshot
# --------------------------------------------------------------------------


def test_a_selected_profile_is_read_from_the_snapshot(tmp_path: Path) -> None:
    repo, sha = _repo_with_profiles(tmp_path)

    config = cli_module._distill_config(
        repo, sha, profile="config/profiles/production.yaml"
    )

    assert config["llm"]["provider"] == "deepseek"
    assert config["llm"]["budget_usd"] == 2.0


def test_the_snapshot_is_preferred_over_the_working_tree(tmp_path: Path) -> None:
    """A profile edited but not committed must not take effect: the run records
    the snapshot sha, so what it ran under has to be in that snapshot."""
    repo, sha = _repo_with_profiles(tmp_path)
    (repo / "config" / "profiles" / "production.yaml").write_text(
        "llm:\n  provider: mock\n  budget_usd: 999.0\n", encoding="utf-8"
    )

    config = cli_module._distill_config(
        repo, sha, profile="config/profiles/production.yaml"
    )

    assert config["llm"]["budget_usd"] == 2.0


def test_the_read_really_goes_through_git_show(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Pinned at the argv boundary: a later refactor that "helpfully" falls back
    to opening the working-tree file would keep every value assertion green."""
    repo, sha = _repo_with_profiles(tmp_path)
    seen: list[str] = []
    real_show = cli_module.GitRepo.show_bytes

    def spy(self: Any, ref_path: str) -> Any:
        seen.append(ref_path)
        return real_show(self, ref_path)

    monkeypatch.setattr(cli_module.GitRepo, "show_bytes", spy)

    cli_module._distill_config(repo, sha, profile="config/profiles/production.yaml")

    # show_bytes specifically: the text channel normalizes line endings, and the
    # provenance hash is taken over whatever comes back from here.
    assert seen == [f"{sha}:config/profiles/production.yaml"]


# --------------------------------------------------------------------------
# Missing profiles: explicit and default differ, deliberately
# --------------------------------------------------------------------------


def test_an_explicit_profile_missing_from_the_snapshot_refuses_to_start(
    tmp_path: Path,
) -> None:
    """Asking for a named profile and silently getting the default is the worst
    failure mode: the run succeeds, on the wrong budget and the wrong model."""
    repo, sha = _repo_with_profiles(tmp_path)

    with pytest.raises(SystemExit, match="not in the frozen snapshot"):
        cli_module._distill_config(repo, sha, profile="config/profiles/absent.yaml")


def test_a_missing_default_profile_still_falls_back(tmp_path: Path) -> None:
    """A brand-new repo, and any non-git working directory, must keep running."""
    repo = tmp_path / "bare"
    repo.mkdir()

    assert cli_module._distill_config(repo, None) == {}


def test_the_default_profile_reads_the_same_bytes_as_before(tmp_path: Path) -> None:
    repo, sha = _repo_with_profiles(tmp_path)

    assert cli_module._distill_config(repo, sha)["llm"]["budget_usd"] == 5.0


# --------------------------------------------------------------------------
# The flag is reachable from the parser
# --------------------------------------------------------------------------


def test_the_config_flag_exists_and_defaults_to_none() -> None:
    parser = cli_module._build_parser()
    action = {
        item.dest: item
        for item in parser._subparsers._group_actions[0]  # type: ignore[union-attr]
        .choices["run"]
        ._actions
    }["config"]

    assert action.default is None


def _args(**overrides: Any) -> Any:
    import argparse

    base: dict[str, Any] = {"config": None}
    base.update(overrides)
    return argparse.Namespace(**base)
