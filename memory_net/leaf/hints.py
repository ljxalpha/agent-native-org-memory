from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from memory_net.shared.adapters import FileSystemMdAdapter


def inject(
    adapter: FileSystemMdAdapter,
    *,
    state_path: Path | None = None,
    config_revision: str = "local",
) -> bool:
    changed = adapter.inject_team_hint(adapter.hint_config)
    if state_path is not None:
        mode = str(adapter.hint_config.get("mode") or "manual")
        _write_state(
            state_path,
            adapter,
            config_revision=config_revision,
            managed_state="disabled_by_mode" if mode == "never" else "installed",
            yaml_sig=hint_config_signature(adapter.hint_config),
        )
    return changed


def remove(
    adapter: FileSystemMdAdapter,
    *,
    state_path: Path | None = None,
    config_revision: str = "local",
) -> bool:
    changed = adapter.remove_team_hint(adapter.hint_config)
    if state_path is not None:
        _write_state(
            state_path,
            adapter,
            config_revision=config_revision,
            managed_state="removed",
            yaml_sig=hint_config_signature(adapter.hint_config),
        )
    return changed


def sync_config_change(
    old_adapter: FileSystemMdAdapter | None,
    new_adapter: FileSystemMdAdapter,
    *,
    state_path: Path,
    config_revision: str,
) -> bool:
    state = _read_state(state_path)
    if state.get("managed_state") != "installed":
        return False

    mode = str(new_adapter.hint_config.get("mode") or "manual")
    old_sig = str(state.get("yaml_sig") or "")
    new_sig = hint_config_signature(new_adapter.hint_config)
    if mode == "never":
        changed = (old_adapter or new_adapter).remove_team_hint(
            (old_adapter or new_adapter).hint_config
        )
        _write_state(
            state_path,
            new_adapter,
            config_revision=config_revision,
            managed_state="disabled_by_mode",
            yaml_sig=new_sig,
        )
        return changed
    if mode != "install_once" or old_sig == new_sig:
        return False

    if old_adapter is not None:
        old_adapter.remove_team_hint(old_adapter.hint_config)
    changed = new_adapter.inject_team_hint(new_adapter.hint_config)
    _write_state(
        state_path,
        new_adapter,
        config_revision=config_revision,
        managed_state="installed",
        yaml_sig=new_sig,
    )
    return changed


def hint_config_signature(config: dict[str, Any]) -> str:
    payload = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _write_state(
    path: Path,
    adapter: FileSystemMdAdapter,
    *,
    config_revision: str,
    managed_state: str,
    yaml_sig: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "adapter": adapter.name,
                "config_revision": config_revision,
                "mode": str(adapter.hint_config.get("mode") or "manual"),
                "managed_state": managed_state,
                "yaml_sig": yaml_sig,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
