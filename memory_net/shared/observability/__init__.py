from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]


def setup_logging(
    level: str | None = None,
    *,
    config_path: Path | None = None,
) -> None:
    config = _load_observability_config(config_path)
    raw_logging_config = config.get("logging") or config.get("log")
    logging_config = raw_logging_config if isinstance(raw_logging_config, dict) else {}
    resolved = (
        level
        or _debug_level_from_env()
        or os.environ.get("MEMORY_NET_LOG_LEVEL")
        or str(logging_config.get("level") or "INFO")
    )
    handlers = _handlers(logging_config)
    if not handlers:
        handlers = [logging.StreamHandler()]
    file_path = logging_config.get("file_path")
    if isinstance(file_path, str) and file_path:
        handlers.append(_file_handler(Path(file_path)))
    secret_filter = _secret_filter(config.get("secrets_filter"))
    if secret_filter is not None:
        for handler in handlers:
            handler.addFilter(secret_filter)
    logging.basicConfig(
        level=getattr(logging, resolved.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
        force=True,
    )


def _handlers(logging_config: dict[str, object]) -> list[logging.Handler]:
    handlers: list[logging.Handler] = []
    sinks = logging_config.get("sinks")
    if not isinstance(sinks, list):
        return handlers
    for sink in sinks:
        if not isinstance(sink, dict):
            continue
        sink_type = str(sink.get("type") or "stderr")
        if sink_type == "stderr":
            handlers.append(logging.StreamHandler())
        elif sink_type == "file" and sink.get("path"):
            handlers.append(_file_handler(Path(str(sink["path"]))))
    return handlers


def _file_handler(path: Path) -> logging.FileHandler:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return logging.FileHandler(path, encoding="utf-8")


def _debug_level_from_env() -> str | None:
    if os.environ.get("LEAF_DEBUG") == "1" or os.environ.get("DISTILLER_DEBUG") == "1":
        return "DEBUG"
    return None


def _load_observability_config(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise RuntimeError(f"observability config must be a mapping: {path}")
    return value


class _SecretsFilter(logging.Filter):
    def __init__(self, *, redact_keys: set[str], hash_keys: set[str]) -> None:
        super().__init__()
        self.redact_keys = redact_keys
        self.hash_keys = hash_keys
        self.patterns = [
            re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
            re.compile(r"\b[Bb]earer\s+[A-Za-z0-9\-._~+/]{16,}=*\b"),
            re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
            re.compile(r"\bsk-ant-(?:api03|admin01)-[A-Za-z0-9_\-]{20,}\b"),
            re.compile(
                r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}" r"T3BlbkFJ[A-Za-z0-9_\-]{20,}\b"
            ),
        ]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        message = self._sanitize_key_values(message)
        for regex in self.patterns:
            message = regex.sub("<REDACTED>", message)
        record.msg = message
        record.args = ()
        return True

    def _sanitize_key_values(self, message: str) -> str:
        keys = self.redact_keys | self.hash_keys
        for key in sorted(keys, key=len, reverse=True):
            pattern = re.compile(
                rf"({re.escape(key)}\s*[:=]\s*[\"']?)([^\"'\s,;]+)",
                re.IGNORECASE,
            )

            # ``key`` is bound as a default so the closure captures this
            # iteration's key rather than the loop variable's final value.
            def replace(match: re.Match[str], key: str = key) -> str:
                value = match.group(2)
                if key in self.hash_keys:
                    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
                    return f"{match.group(1)}<HASH:{digest}>"
                return f"{match.group(1)}<REDACTED>"

            message = pattern.sub(replace, message)
        return message


def _secret_filter(raw: object) -> logging.Filter | None:
    if not isinstance(raw, dict):
        return None
    redact_keys = {
        str(item)
        for item in raw.get("redact_keys", raw.get("redact", [])) or []
        if str(item)
    }
    hash_keys = {
        str(item)
        for item in raw.get("hash_keys", raw.get("hash", [])) or []
        if str(item)
    }
    if bool(raw.get("deny_by_default", False)):
        redact_keys.update({"api_key", "token", "secret", "password", "authorization"})
    return _SecretsFilter(redact_keys=redact_keys, hash_keys=hash_keys)
