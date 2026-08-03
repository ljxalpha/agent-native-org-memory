#!/usr/bin/env python3
from __future__ import annotations

import argparse
import difflib
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diff team products between two distill runs."
    )
    parser.add_argument("repo", type=Path)
    parser.add_argument("base_run")
    parser.add_argument("head_run")
    parser.add_argument("--base-ref")
    parser.add_argument("--head-ref")
    parser.add_argument("--branch-template", default="release/<run_id>")
    args = parser.parse_args()
    print(
        diff_runs(
            args.repo,
            args.base_run,
            args.head_run,
            base_ref=args.base_ref,
            head_ref=args.head_ref,
            branch_template=args.branch_template,
        ),
        end="",
    )
    return 0


def diff_runs(
    repo: Path,
    base_run: str,
    head_run: str,
    *,
    base_ref: str | None = None,
    head_ref: str | None = None,
    branch_template: str = "release/<run_id>",
) -> str:
    base = _read_run_products(
        repo,
        base_run,
        explicit_ref=base_ref,
        branch_template=branch_template,
    )
    head = _read_run_products(
        repo,
        head_run,
        explicit_ref=head_ref,
        branch_template=branch_template,
    )
    lines: list[str] = []
    for path in sorted(set(base) | set(head)):
        if base.get(path) == head.get(path):
            continue
        lines.extend(
            difflib.unified_diff(
                base.get(path, "").splitlines(keepends=True),
                head.get(path, "").splitlines(keepends=True),
                fromfile=f"{base_run}/{path}",
                tofile=f"{head_run}/{path}",
            )
        )
    return "".join(lines) or "No product changes\n"


def _read_run_products(
    repo: Path,
    run_id: str,
    *,
    explicit_ref: str | None,
    branch_template: str,
) -> dict[str, str]:
    if explicit_ref:
        return _read_run_products_from_ref(repo, explicit_ref, run_id)
    root = repo / "distill_runs" / run_id / "team"
    if not root.exists():
        refs = []
        branch = branch_template.replace("<run_id>", run_id)
        refs.extend([branch, f"origin/{branch}"])
        for ref in refs:
            try:
                return _read_run_products_from_ref(repo, ref, run_id)
            except FileNotFoundError:
                continue
        raise FileNotFoundError(f"distill run has no product snapshot: {run_id}")
    products: dict[str, str] = {}
    for path in sorted(root.rglob("*.md")):
        products[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
    return products


def _read_run_products_from_ref(repo: Path, ref: str, run_id: str) -> dict[str, str]:
    prefix = f"distill_runs/{run_id}/team"
    result = _git(repo, "ls-tree", "-r", "--name-only", ref, prefix)
    if result.returncode != 0 or not result.stdout.strip():
        raise FileNotFoundError(f"distill run has no product snapshot: {run_id}")
    products: dict[str, str] = {}
    for rel in sorted(
        line for line in result.stdout.splitlines() if line.endswith(".md")
    ):
        content = _git(repo, "show", f"{ref}:{rel}")
        if content.returncode != 0:
            raise FileNotFoundError(f"cannot read product from {ref}: {rel}")
        products[Path(rel).relative_to(prefix).as_posix()] = content.stdout
    return products


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


if __name__ == "__main__":
    raise SystemExit(main())
