from pathlib import Path

from memory_net.shared.config import (
    canonical_generation_hash,
    materialize_generation,
    subscribe_contract_revision,
)


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
        "\n".join(
            [
                "name: claude-code",
                "team_target: /tmp/team-a",
                "roots:",
                "  shared:",
                "    - alias: shared",
                "      base_dir: /tmp/shared-a",
                "team_hint_injection:",
                "  mode: install_once",
            ]
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
        "\n".join(
            [
                "name: claude-code",
                "roots:",
                "  shared:",
                "    - alias: changed",
                "      base_dir: /tmp/shared-b",
                "team_hint_injection:",
                "  mode: never",
                "team_target: /tmp/team-a",
            ]
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
