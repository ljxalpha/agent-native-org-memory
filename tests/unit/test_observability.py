import logging
from pathlib import Path
from typing import Any

from memory_net.shared.observability import setup_logging


def test_setup_logging_loads_config_level(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.delenv("MEMORY_NET_LOG_LEVEL", raising=False)
    monkeypatch.delenv("LEAF_DEBUG", raising=False)
    monkeypatch.delenv("DISTILLER_DEBUG", raising=False)
    config = tmp_path / "observability.yaml"
    config.write_text("logging:\n  level: WARNING\n", encoding="utf-8")

    setup_logging(config_path=config)

    assert logging.getLogger().level == logging.WARNING


def test_setup_logging_env_overrides_config(tmp_path: Path, monkeypatch: Any) -> None:
    config = tmp_path / "observability.yaml"
    config.write_text("logging:\n  level: WARNING\n", encoding="utf-8")
    monkeypatch.setenv("LEAF_DEBUG", "1")

    setup_logging(config_path=config)

    assert logging.getLogger().level == logging.DEBUG


def test_setup_logging_configures_file_sink(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.delenv("MEMORY_NET_LOG_LEVEL", raising=False)
    monkeypatch.delenv("LEAF_DEBUG", raising=False)
    monkeypatch.delenv("DISTILLER_DEBUG", raising=False)
    log_path = tmp_path / "run.log"
    config = tmp_path / "observability.yaml"
    config.write_text(
        f"log:\n  level: INFO\n  sinks:\n    - type: file\n      path: {log_path}\n",
        encoding="utf-8",
    )

    setup_logging(config_path=config)
    logging.getLogger("memory_net.test").info("configured sink")

    assert "configured sink" in log_path.read_text(encoding="utf-8")


def test_setup_logging_installs_secret_filter(tmp_path: Path) -> None:
    log_path = tmp_path / "run.log"
    config = tmp_path / "observability.yaml"
    config.write_text(
        "\n".join(
            [
                "logging:",
                "  level: INFO",
                "  sinks:",
                "    - type: file",
                f"      path: {log_path}",
                "secrets_filter:",
                "  deny_by_default: true",
            ]
        ),
        encoding="utf-8",
    )

    setup_logging(config_path=config)
    logging.getLogger("memory_net.test").info(
        "token=abc12345678901234567890 bearer Bearer abc12345678901234567890"
    )

    content = log_path.read_text(encoding="utf-8")
    assert "abc12345678901234567890" not in content
    assert "<REDACTED>" in content
