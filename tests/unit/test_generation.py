from pathlib import Path

import pytest

from memory_net.shared.config import (
    canonical_generation_hash,
    materialize_generation,
    subscribe_contract_revision,
)
from memory_net.shared.config.generation import GENERATION_MARKER_FILES


def test_canonical_generation_hash_maps_config_to_generation_root(
    tmp_path: Path,
) -> None:
    (tmp_path / "config" / "adapters").mkdir(parents=True)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "bootstrap_state.json").write_text("{}", encoding="utf-8")
    (tmp_path / "config" / "subscribe.yaml").write_text("a: 1\n", encoding="utf-8")
    (tmp_path / "config" / "adapters" / "claude-code.yaml").write_text(
        "name: claude-code\nteam_target: /tmp/team\n",
        encoding="utf-8",
    )
    (tmp_path / "prompts" / "distill.md").write_text("prompt\n", encoding="utf-8")

    first = canonical_generation_hash(tmp_path)
    second = canonical_generation_hash(tmp_path)

    assert first == second
    generation = materialize_generation(
        tmp_path,
        tmp_path / "generations",
        adapter_name="claude-code",
    )
    assert (generation / "subscribe.yaml").exists()
    assert (generation / "subscribe_contract_revision").read_text(
        encoding="utf-8"
    ) == subscribe_contract_revision(tmp_path, adapter_name="claude-code")
    assert canonical_generation_hash(generation) == first
    assert (generation / "prompts" / "distill.md").exists()


def test_subscribe_contract_revision_is_agent_specific(tmp_path: Path) -> None:
    adapters = tmp_path / "config" / "adapters"
    adapters.mkdir(parents=True)
    (tmp_path / "bootstrap_state.json").write_text("{}", encoding="utf-8")
    (tmp_path / "config" / "subscribe.yaml").write_text("a: 1\n", encoding="utf-8")
    (adapters / "claude-code.yaml").write_text(
        (
            "name: claude-code\n"
            "team_target: /tmp/team-a\n"
            "roots:\n"
            "  shared:\n"
            "    - alias: shared\n"
            "      base_dir: /tmp/shared-a\n"
            "team_hint_injection:\n"
            "  mode: install_once"
        ),
        encoding="utf-8",
    )
    (adapters / "hermes.yaml").write_text(
        "name: hermes\nteam_target: /tmp/team-b\n",
        encoding="utf-8",
    )

    first = subscribe_contract_revision(tmp_path, adapter_name="claude-code")
    (adapters / "hermes.yaml").write_text(
        "name: hermes\nteam_target: /tmp/team-c\n",
        encoding="utf-8",
    )
    after_unrelated_change = subscribe_contract_revision(
        tmp_path,
        adapter_name="claude-code",
    )
    (adapters / "claude-code.yaml").write_text(
        (
            "name: claude-code\n"
            "roots:\n"
            "  shared:\n"
            "    - alias: changed\n"
            "      base_dir: /tmp/shared-b\n"
            "team_hint_injection:\n"
            "  mode: never\n"
            "team_target: /tmp/team-a"
        ),
        encoding="utf-8",
    )
    after_active_non_subscriber_change = subscribe_contract_revision(
        tmp_path,
        adapter_name="claude-code",
    )
    (adapters / "claude-code.yaml").write_text(
        "team_target:    /tmp/team-a\nname: claude-code\n",
        encoding="utf-8",
    )
    after_formatting_change = subscribe_contract_revision(
        tmp_path,
        adapter_name="claude-code",
    )
    (adapters / "claude-code.yaml").write_text(
        "name: claude-code\nteam_target: /tmp/team-d\n",
        encoding="utf-8",
    )
    after_active_change = subscribe_contract_revision(
        tmp_path,
        adapter_name="claude-code",
    )

    assert after_unrelated_change == first
    assert after_active_non_subscriber_change == first
    assert after_formatting_change == first
    assert after_active_change != first


def test_materialize_generation_quarantines_corrupted_existing_generation(
    tmp_path: Path,
) -> None:
    (tmp_path / "config" / "adapters").mkdir(parents=True)
    (tmp_path / "bootstrap_state.json").write_text("{}", encoding="utf-8")
    (tmp_path / "config" / "subscribe.yaml").write_text("a: 1\n", encoding="utf-8")
    (tmp_path / "config" / "adapters" / "claude-code.yaml").write_text(
        "name: claude-code\nteam_target: /tmp/team\n",
        encoding="utf-8",
    )
    generations = tmp_path / "generations"
    revision = canonical_generation_hash(tmp_path)
    corrupted = generations / revision
    corrupted.mkdir(parents=True)
    (corrupted / "subscribe.yaml").write_text("corrupted: true\n", encoding="utf-8")

    generation = materialize_generation(
        tmp_path,
        generations,
        adapter_name="claude-code",
    )

    quarantined = list((generations / "_corrupted").glob(f"{revision}-actual-*"))
    assert generation == generations / revision
    assert (generation / "subscribe.yaml").read_text(encoding="utf-8") == "a: 1\n"
    assert quarantined
    assert (quarantined[0] / "subscribe.yaml").read_text(
        encoding="utf-8"
    ) == "corrupted: true\n"


def test_materialize_generation_does_not_write_master_sha_marker_by_default(
    tmp_path: Path,
) -> None:
    """Regression: callers that do not supply provenance keep the old layout."""
    _config_tree(tmp_path)

    generation = materialize_generation(
        tmp_path,
        tmp_path / "generations",
        adapter_name="claude-code",
    )

    assert not (generation / "config_source_master_sha").exists()


def test_materialize_generation_records_source_master_sha(tmp_path: Path) -> None:
    _config_tree(tmp_path)

    generation = materialize_generation(
        tmp_path,
        tmp_path / "generations",
        adapter_name="claude-code",
        source_master_sha="a" * 40,
    )

    assert (generation / "config_source_master_sha").read_text(
        encoding="utf-8"
    ) == "a" * 40


def test_config_revision_is_independent_of_the_source_master_sha(
    tmp_path: Path,
) -> None:
    """The provenance marker must never feed the content-addressed hash.

    If it did, one unchanged config would land in two different generation
    directories whenever master moved, defeating reuse and re-verification.
    """
    first_source = tmp_path / "first"
    second_source = tmp_path / "second"
    _config_tree(first_source)
    _config_tree(second_source)
    generations = tmp_path / "generations"

    first = materialize_generation(
        first_source,
        generations,
        adapter_name="claude-code",
        source_master_sha="a" * 40,
    )
    second = materialize_generation(
        second_source,
        generations,
        adapter_name="claude-code",
        source_master_sha="b" * 40,
    )

    assert first.name == second.name
    assert first == second
    assert canonical_generation_hash(first) == first.name
    assert (first / "config_source_master_sha").read_text(encoding="utf-8") == "b" * 40


def test_reused_generation_refreshes_a_stale_master_sha_marker(
    tmp_path: Path,
) -> None:
    _config_tree(tmp_path)
    generations = tmp_path / "generations"
    materialize_generation(
        tmp_path,
        generations,
        adapter_name="claude-code",
        source_master_sha="a" * 40,
    )

    generation = materialize_generation(
        tmp_path,
        generations,
        adapter_name="claude-code",
        source_master_sha="b" * 40,
    )

    assert (generation / "config_source_master_sha").read_text(
        encoding="utf-8"
    ) == "b" * 40
    assert canonical_generation_hash(generation) == generation.name


def _config_tree(root: Path) -> None:
    (root / "config" / "adapters").mkdir(parents=True)
    (root / "bootstrap_state.json").write_text("{}", encoding="utf-8")
    (root / "config" / "subscribe.yaml").write_text("a: 1\n", encoding="utf-8")
    (root / "config" / "adapters" / "claude-code.yaml").write_text(
        "name: claude-code\nteam_target: /tmp/team\n",
        encoding="utf-8",
    )


def test_markers_under_a_config_source_layout_are_not_hashed(tmp_path: Path) -> None:
    """A source tree with a ``config/`` dir is walked with rglob, so the marker
    exclusion has to be explicit there or provenance would enter the hash."""
    _config_tree(tmp_path)
    baseline = canonical_generation_hash(tmp_path)

    for marker in GENERATION_MARKER_FILES:
        (tmp_path / "config" / marker).write_text("x" * 40, encoding="utf-8")

    assert canonical_generation_hash(tmp_path) == baseline


def test_markers_under_a_flat_source_layout_are_not_hashed(tmp_path: Path) -> None:
    """The materialized-generation layout is flat; markers live at its root."""
    (tmp_path / "adapters").mkdir(parents=True)
    (tmp_path / "bootstrap_state.json").write_text("{}", encoding="utf-8")
    (tmp_path / "subscribe.yaml").write_text("a: 1\n", encoding="utf-8")
    (tmp_path / "adapters" / "claude-code.yaml").write_text(
        "name: claude-code\n", encoding="utf-8"
    )
    baseline = canonical_generation_hash(tmp_path)

    for marker in GENERATION_MARKER_FILES:
        (tmp_path / marker).write_text("x" * 40, encoding="utf-8")

    assert canonical_generation_hash(tmp_path) == baseline


def test_a_nested_file_named_like_a_marker_is_still_hashed(tmp_path: Path) -> None:
    """Only the root markers are provenance; a nested same-named file is content."""
    _config_tree(tmp_path)
    baseline = canonical_generation_hash(tmp_path)

    (tmp_path / "config" / "adapters" / "config_revision").write_text(
        "content\n", encoding="utf-8"
    )

    assert canonical_generation_hash(tmp_path) != baseline


def test_source_master_sha_marker_write_leaves_no_temporary_file(
    tmp_path: Path,
) -> None:
    """The marker is replaced atomically, so no ``.tmp`` residue may survive."""
    _config_tree(tmp_path)
    generations = tmp_path / "generations"

    generation = materialize_generation(
        tmp_path,
        generations,
        adapter_name="claude-code",
        source_master_sha="a" * 40,
    )
    materialize_generation(
        tmp_path,
        generations,
        adapter_name="claude-code",
        source_master_sha="c" * 40,
    )

    assert [path.name for path in generation.glob(".*tmp")] == []
    assert (generation / "config_source_master_sha").read_text(
        encoding="utf-8"
    ) == "c" * 40


def test_a_failed_marker_refresh_leaves_the_previous_sha_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refresh must be all-or-nothing on a shared, reusable generation.

    A direct write would truncate the marker before writing, so an interrupted
    refresh would leave an empty or partial sha behind for whoever reads it next.
    """
    _config_tree(tmp_path)
    generations = tmp_path / "generations"
    generation = materialize_generation(
        tmp_path,
        generations,
        adapter_name="claude-code",
        source_master_sha="a" * 40,
    )

    def exploding_replace(self: Path, _target: object) -> Path:
        raise OSError("interrupted")

    monkeypatch.setattr(Path, "replace", exploding_replace)
    with pytest.raises(OSError, match="interrupted"):
        materialize_generation(
            tmp_path,
            generations,
            adapter_name="claude-code",
            source_master_sha="b" * 40,
        )

    assert (generation / "config_source_master_sha").read_text(
        encoding="utf-8"
    ) == "a" * 40
