"""Tests for operational identity collection.

Every host value here is a neutral placeholder: ``platform.node()`` is always
monkeypatched so no real machine name can reach a test artifact.
"""

from __future__ import annotations

import ast
import platform
from pathlib import Path

import pytest

import memory_net
from memory_net.leaf.identity import (
    OpsIdentityError,
    collect_ops_identity,
    read_host,
)

PLACEHOLDER_HOST = "leaf-host-01"
SAMPLE_SHA = "0123456789abcdef0123456789abcdef01234567"
STARTED_AT = "2026-04-20T09:00:00Z"


@pytest.fixture(autouse=True)
def _neutral_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "node", lambda: PLACEHOLDER_HOST)


def test_collects_every_source(tmp_path: Path) -> None:
    layout = _layout(tmp_path, master_sha="f" * 40)

    identity = collect_ops_identity(**layout, started_at=STARTED_AT)

    assert identity == {
        "install_id": "inst-0001",
        "code_version": memory_net.__version__,
        "build_id": SAMPLE_SHA,
        "build_id_source": "package_ref",
        "host": PLACEHOLDER_HOST,
        "adapter": "claude-code",
        "config_source_master_sha": "f" * 40,
        "started_at": STARTED_AT,
    }


def test_install_id_is_stripped(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    (layout["state_dir"] / "install_id").write_text("  inst-0002\n", encoding="utf-8")

    identity = collect_ops_identity(**layout, started_at=STARTED_AT)

    assert identity["install_id"] == "inst-0002"


def test_missing_install_id_is_rejected(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    (layout["state_dir"] / "install_id").unlink()

    with pytest.raises(OpsIdentityError, match="install_id"):
        collect_ops_identity(**layout, started_at=STARTED_AT)


def test_empty_install_id_is_rejected(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    (layout["state_dir"] / "install_id").write_text("\n", encoding="utf-8")

    with pytest.raises(OpsIdentityError, match="install_id"):
        collect_ops_identity(**layout, started_at=STARTED_AT)


def test_missing_adapter_yaml_is_rejected(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    layout["adapter_path"].unlink()

    with pytest.raises(OpsIdentityError, match="adapter"):
        collect_ops_identity(**layout, started_at=STARTED_AT)


def test_adapter_yaml_without_name_is_rejected(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    layout["adapter_path"].write_text("roots: {}\n", encoding="utf-8")

    with pytest.raises(OpsIdentityError, match="adapter"):
        collect_ops_identity(**layout, started_at=STARTED_AT)


def test_missing_started_at_is_rejected(tmp_path: Path) -> None:
    layout = _layout(tmp_path)

    with pytest.raises(OpsIdentityError, match="started_at"):
        collect_ops_identity(**layout, started_at="")


def test_empty_host_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform, "node", lambda: "")
    layout = _layout(tmp_path)

    with pytest.raises(OpsIdentityError, match="host"):
        collect_ops_identity(**layout, started_at=STARTED_AT)


@pytest.mark.parametrize(
    "package_ref",
    [
        "",
        "memory-net",
        "/local/path/to/aomt",
        "git+ssh://git@example.invalid/org/aomt.git@main",
        f"git+ssh://git@example.invalid/org/aomt.git@{SAMPLE_SHA[:39]}",
    ],
)
def test_build_id_falls_back_to_code_version_with_audit_trail(
    tmp_path: Path, package_ref: str
) -> None:
    layout = _layout(tmp_path, package_ref=package_ref)

    identity = collect_ops_identity(**layout, started_at=STARTED_AT)

    assert identity["build_id"] == memory_net.__version__
    assert identity["build_id_source"] == "code_version"


def test_missing_local_yaml_falls_back_to_code_version(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    layout["local_yaml_path"].unlink()

    identity = collect_ops_identity(**layout, started_at=STARTED_AT)

    assert identity["build_id"] == memory_net.__version__
    assert identity["build_id_source"] == "code_version"


def test_config_source_master_sha_key_absent_when_marker_missing(
    tmp_path: Path,
) -> None:
    """Backward compatibility with generations materialized before the marker."""
    layout = _layout(tmp_path)

    identity = collect_ops_identity(**layout, started_at=STARTED_AT)

    assert "config_source_master_sha" not in identity


def test_config_source_master_sha_is_stripped(tmp_path: Path) -> None:
    layout = _layout(tmp_path, master_sha="a" * 40)
    marker = layout["generation_dir"] / "config_source_master_sha"
    marker.write_text("a" * 40 + "\n", encoding="utf-8")

    identity = collect_ops_identity(**layout, started_at=STARTED_AT)

    assert identity["config_source_master_sha"] == "a" * 40


def test_blank_config_source_master_sha_marker_is_ignored(tmp_path: Path) -> None:
    layout = _layout(tmp_path, master_sha="   \n")

    identity = collect_ops_identity(**layout, started_at=STARTED_AT)

    assert "config_source_master_sha" not in identity


#: Every way to obtain the local machine name that the guard below must catch.
#: Matching only the literal ``platform.node()`` shape would leave the whole
#: family of equivalents (aliased imports, ``socket``, ``os.uname``, the
#: environment) as open doors around a privacy invariant.
HOSTNAME_SOURCES = {
    ("platform", "node"),
    ("platform", "uname"),
    ("socket", "gethostname"),
    ("socket", "getfqdn"),
    ("os", "uname"),
}
HOSTNAME_ENV_VARS = {"HOSTNAME", "HOST", "COMPUTERNAME"}


class _HostnameReadFinder(ast.NodeVisitor):
    """Collect every hostname read in one module, resolving import aliases."""

    def __init__(self) -> None:
        #: alias -> canonical module name, e.g. ``{"p": "platform"}``
        self.module_aliases: dict[str, str] = {}
        #: alias -> (module, attribute) for ``from platform import node as n``
        self.symbol_aliases: dict[str, tuple[str, str]] = {}
        self.hits: list[int] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name in {module for module, _ in HOSTNAME_SOURCES}:
                self.module_aliases[alias.asname or alias.name] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if (node.module, alias.name) in HOSTNAME_SOURCES:
                assert node.module is not None
                self.symbol_aliases[alias.asname or alias.name] = (
                    node.module,
                    alias.name,
                )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        bare_alias = isinstance(func, ast.Name) and func.id in self.symbol_aliases
        qualified = (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and (self.module_aliases.get(func.value.id), func.attr) in HOSTNAME_SOURCES
        )
        if bare_alias or qualified:
            self.hits.append(node.lineno)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        # ``os.environ["HOSTNAME"]`` / ``os.getenv("HOSTNAME")`` read the same
        # material without calling any of the APIs above.
        if isinstance(node.value, str) and node.value in HOSTNAME_ENV_VARS:
            self.hits.append(node.lineno)
        self.generic_visit(node)


def test_the_hostname_is_read_in_exactly_one_module() -> None:
    """AST guard: the hostname may be read in exactly one module.

    ``host`` is local machine identity; confining the read to a single call site
    is what makes it auditable where it flows.
    """
    package_root = Path(memory_net.__file__).resolve().parent
    modules: list[str] = []
    for path in sorted(package_root.rglob("*.py")):
        finder = _HostnameReadFinder()
        finder.visit(ast.parse(path.read_text(encoding="utf-8")))
        modules.extend(
            f"{path.relative_to(package_root)}:{line}" for line in finder.hits
        )
    assert [site.split(":")[0] for site in modules] == ["leaf/identity.py"], modules
    assert read_host() == PLACEHOLDER_HOST


@pytest.mark.parametrize(
    "source",
    [
        "import platform\nplatform.node()\n",
        "import platform as p\np.node()\n",
        "from platform import node\nnode()\n",
        "from platform import node as n\nn()\n",
        "import platform\nplatform.uname().node\n",
        "import socket\nsocket.gethostname()\n",
        "import socket\nsocket.getfqdn()\n",
        "import os\nos.uname().nodename\n",
        'import os\nos.environ["HOSTNAME"]\n',
        'import os\nos.getenv("COMPUTERNAME")\n',
    ],
)
def test_the_hostname_guard_catches_every_equivalent_read(source: str) -> None:
    """Each of these would otherwise be an unaudited hostname read."""
    finder = _HostnameReadFinder()
    finder.visit(ast.parse(source))

    assert finder.hits, source


def _layout(
    tmp_path: Path,
    *,
    package_ref: str = f"git+ssh://git@example.invalid/org/aomt.git@{SAMPLE_SHA}",
    master_sha: str | None = None,
) -> dict[str, Path]:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "install_id").write_text("inst-0001\n", encoding="utf-8")

    adapter_path = tmp_path / "adapters" / "claude-code.yaml"
    adapter_path.parent.mkdir(parents=True)
    adapter_path.write_text("name: claude-code\nroots: {}\n", encoding="utf-8")

    local_yaml_path = tmp_path / "local.yaml"
    local_yaml_path.write_text(
        f'node_id: "alice"\npackage_ref: "{package_ref}"\n', encoding="utf-8"
    )

    generation_dir = tmp_path / "generation"
    generation_dir.mkdir()
    if master_sha is not None:
        (generation_dir / "config_source_master_sha").write_text(
            master_sha, encoding="utf-8"
        )

    return {
        "state_dir": state_dir,
        "adapter_path": adapter_path,
        "local_yaml_path": local_yaml_path,
        "generation_dir": generation_dir,
    }
