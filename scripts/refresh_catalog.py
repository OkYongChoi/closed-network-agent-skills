#!/usr/bin/env python3
"""Refresh deterministic skill tree digests in catalog.json."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "catalog.json"
SKILLS = ROOT / "skills"
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _load_installer():
    path = ROOT / "skills/skills-installer/scripts/skill_installer.py"
    spec = importlib.util.spec_from_file_location("catalog_skill_installer", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load installer digest implementation: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


INSTALLER = _load_installer()


def digest_tree(root: Path) -> str:
    files = INSTALLER.inspect_tree(
        root, INSTALLER.DEFAULT_MAX_FILES, INSTALLER.DEFAULT_MAX_BYTES
    )
    return INSTALLER.tree_digest(files)


def frontmatter_description(skill_file: Path) -> str:
    lines = skill_file.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        raise ValueError(f"missing frontmatter: {skill_file}")
    for line in lines[1:]:
        if line == "---":
            break
        if line.startswith("description:"):
            value = line.split(":", 1)[1].strip()
            if value.startswith('"'):
                parsed = json.loads(value)
                if not isinstance(parsed, str):
                    raise ValueError(f"description is not a string: {skill_file}")
                return parsed
            return value
    raise ValueError(f"missing description: {skill_file}")


def render_catalog() -> bytes:
    try:
        current = json.loads(CATALOG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        current = {}
    repository = current.get(
        "repository",
        {
            "url": "https://github.com/OkYongChoi/skills.git",
            "ref": None,
            "ref_status": "pending-initial-publish",
        },
    )
    items = []
    for skill in sorted(SKILLS.iterdir(), key=lambda path: path.name):
        if not skill.is_dir() or skill.is_symlink():
            continue
        if not NAME_RE.fullmatch(skill.name):
            raise ValueError(f"invalid skill directory name: {skill.name}")
        items.append(
            {
                "name": skill.name,
                "description": frontmatter_description(skill / "SKILL.md"),
                "path": f"skills/{skill.name}",
                "sha256": digest_tree(skill),
            }
        )
    output = {"format_version": 1, "repository": repository, "skills": items}
    return (json.dumps(output, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        expected = render_catalog()
        if args.check:
            actual = CATALOG.read_bytes()
            if actual != expected:
                print("catalog.json is stale; run scripts/refresh_catalog.py", file=sys.stderr)
                return 1
        else:
            CATALOG.write_bytes(expected)
        return 0
    except (OSError, ValueError, json.JSONDecodeError, INSTALLER.InstallError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
