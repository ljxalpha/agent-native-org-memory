"""Distillation profile validation and provenance (§5.5.6).

Two things live here, both pure: no IO, no environment reads, no logging. The
CLI does the reading and the exiting; this module only decides what a profile
means and whether it is allowed to run.

The point of the validation is that **production has no defaults**. Every value
this module insists on has a default elsewhere in the CLI that is right for a
mock run and wrong for a real one -- a mock provider, Anthropic pricing applied
to a DeepSeek endpoint, a three-dimensional stand-in for an embedding. Each of
those failures is silent: the run succeeds, the metrics look ordinary, and the
products are worthless. Refusing to start is the only signal available.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

PRODUCTION = "production"
DEVELOPMENT = "development"
PROFILE_CLASSES = (PRODUCTION, DEVELOPMENT)

OUTPUT_SCANNER_MODES = ("enforce", "observe")
"""Output-side scanner modes.

Deliberately disjoint from the leaf-side ``audit`` / ``warn`` / ``block``
(``memory_net/leaf/scanner.py``). The two act on different things -- a record
being reported versus model output about to be persisted -- and sharing a
vocabulary is how someone eventually reads the leaf side's ``audit`` as the
output side's ``observe``.
"""

FORBIDDEN_PRODUCTION_PROVIDERS = ("mock", "hermes_tool")
"""Backends that must never run production distillation (§3.0).

``mock`` is not a model at all. ``hermes_tool`` reaches its backend through a
CLI that takes a single text prompt, so the system/user role boundary does not
survive the wire and there is no service-level structured-output constraint --
neither property is optional when the input is untrusted leaf records.
"""

REQUIRED_PRODUCTION_KEYS = (
    "llm.provider",
    "llm.model",
    "llm.pricing.input_per_mtok_usd",
    "llm.pricing.output_per_mtok_usd",
    "llm.budget_usd",
    "clustering.embedding_backend",
    "output_scanner.schema_version",
    "output_scanner.mode",
)


class ProfileError(ValueError):
    """A profile that must not be allowed to start a run."""


@dataclass(frozen=True)
class ProfileProvenance:
    """Which profile bytes a run actually used.

    ``blob_sha256`` hashes the *raw bytes*, not the parsed mapping: the question
    it answers is "which bytes did this run read", and parsing discards comments,
    key order and formatting -- so two visibly different files could otherwise
    produce the same provenance.
    """

    profile_path: str
    blob_sha256: str
    master_snapshot_sha: str

    @classmethod
    def of(
        cls, *, profile_path: str, raw: str, master_snapshot_sha: str | None
    ) -> ProfileProvenance:
        return cls(
            profile_path=profile_path,
            blob_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            # "" rather than None: this lands in JSON next to other string
            # fields, and a null there reads as "unknown" instead of "no
            # snapshot", which is what a non-git working directory means.
            master_snapshot_sha=master_snapshot_sha or "",
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "master_snapshot_sha": self.master_snapshot_sha,
            "profile_blob_sha256": self.blob_sha256,
            "profile_path": self.profile_path,
        }


def profile_class(config: dict[str, Any]) -> str:
    """The profile's own declaration of what it is for.

    Absent means development, because the shipped default profile does not
    carry the key and must keep running exactly as it does today.
    """
    raw = config.get("profile_class")
    if raw is None:
        return DEVELOPMENT
    value = str(raw)
    if value not in PROFILE_CLASSES:
        raise ProfileError(
            f"profile_class must be one of {PROFILE_CLASSES}, got {value!r}"
        )
    return value


def validate_profile(config: dict[str, Any]) -> str:
    """Check a profile and return its class, or raise ``ProfileError``."""
    declared = profile_class(config)
    _validate_output_scanner_mode(config)
    if declared != PRODUCTION:
        return declared
    missing = [key for key in REQUIRED_PRODUCTION_KEYS if _lookup(config, key) is None]
    if missing:
        raise ProfileError(
            "production profile is missing required keys: " + ", ".join(missing)
        )
    provider = str(_lookup(config, "llm.provider"))
    if provider in FORBIDDEN_PRODUCTION_PROVIDERS:
        raise ProfileError(
            f"production profile cannot use llm.provider={provider!r}; "
            f"forbidden: {', '.join(FORBIDDEN_PRODUCTION_PROVIDERS)}"
        )
    mode = str(_lookup(config, "output_scanner.mode"))
    if mode != "enforce":
        # Not silently corrected to "enforce": a correction nobody sees leaves
        # the author believing the mode they wrote is the one in effect.
        raise ProfileError(
            f"production profile requires output_scanner.mode=enforce, got {mode!r}"
        )
    return declared


def _validate_output_scanner_mode(config: dict[str, Any]) -> None:
    """Reject an out-of-domain mode in *any* profile class.

    Enforced outside production too, because the value most likely to be written
    here by mistake is one of the leaf side's, and letting it sit unchallenged
    in a development profile is how it later gets copied into a production one.
    """
    mode = _lookup(config, "output_scanner.mode")
    if mode is None:
        return
    if str(mode) not in OUTPUT_SCANNER_MODES:
        raise ProfileError(
            f"output_scanner.mode must be one of {OUTPUT_SCANNER_MODES}, "
            f"got {mode!r} (the leaf-side audit/warn/block vocabulary is a "
            "different thing and is not accepted here)"
        )


def _lookup(config: dict[str, Any], dotted: str) -> Any:
    """Read a dotted key, returning ``None`` when any segment is absent.

    An explicit ``null`` in the YAML is treated as absent on purpose: writing
    ``budget_usd:`` with no value declares nothing, and reading it as "declared"
    would let a production profile satisfy the check with an empty promise.
    """
    current: Any = config
    for part in dotted.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current
