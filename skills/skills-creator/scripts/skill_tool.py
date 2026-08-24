#!/usr/bin/env python3
"""Create and validate skills without third-party Python packages.

Adapted from OpenAI's skill-creator. Modified for a restricted portable profile,
atomic creation, and offline use. See THIRD_PARTY_NOTICES.md at repository root.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import portable_paths

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ALLOWED_FIELDS = {"name", "description", "license", "compatibility"}
PLAIN_SCALAR_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._/-]*$")
YAML_RESERVED = {
    "null", "true", "false", "yes", "no", "on", "off", "y", "n", "~",
    ".nan", ".inf", "+.inf", "-.inf",
}
IGNORED_RUNTIME_DIRS = {"__pycache__"}
IGNORED_RUNTIME_FILES = {".DS_Store"}


class SkillError(ValueError):
    """Raised when a skill violates the restricted authoring profile."""


def validate_name(name: str) -> None:
    if not (1 <= len(name) <= 64) or not NAME_RE.fullmatch(name):
        raise SkillError(
            "name must be 1-64 lowercase letters, digits, or single hyphens"
        )
    try:
        portable_paths.validate_portable_component(name, label="skill name")
    except portable_paths.PortablePathError as exc:
        raise SkillError(str(exc)) from exc


def validate_tree_paths(root: Path) -> None:
    """Reject names that collide or cannot be checked out on Windows."""

    seen: dict[str, str] = {}
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise SkillError(f"cannot scan skill tree: {exc}") from exc
        for entry in entries:
            if (
                entry.name in IGNORED_RUNTIME_DIRS
                or entry.name in IGNORED_RUNTIME_FILES
                or entry.name.endswith((".pyc", ".pyo"))
            ):
                continue
            path = Path(entry.path)
            rel = path.relative_to(root).as_posix()
            try:
                pure = portable_paths.validate_portable_relative_path(
                    rel, label="skill path"
                )
            except portable_paths.PortablePathError as exc:
                raise SkillError(str(exc)) from exc
            key = portable_paths.portable_path_key(pure)
            previous = seen.setdefault(key, rel)
            if previous != rel:
                raise SkillError(
                    f"normalization/case-colliding paths: {previous!r} and {rel!r}"
                )
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise SkillError(f"cannot inspect skill path {rel!r}: {exc}") from exc
            if entry.is_symlink() or portable_paths.is_windows_reparse_point(
                entry_stat
            ):
                raise SkillError(
                    f"symbolic links and reparse points are not allowed: {rel}"
                )
            if entry.is_dir(follow_symlinks=False):
                stack.append(path)
            elif not entry.is_file(follow_symlinks=False):
                raise SkillError(f"special files are not allowed: {rel}")
            else:
                try:
                    entry_stat = portable_paths.full_file_stat(path, entry_stat)
                    if portable_paths.is_windows_reparse_point(entry_stat):
                        raise portable_paths.PortablePathError(
                            f"reparse points are not allowed: {rel}"
                        )
                    link_count = portable_paths.file_link_count(path, entry_stat)
                except portable_paths.PortablePathError as exc:
                    raise SkillError(str(exc)) from exc
                if link_count != 1:
                    raise SkillError(f"hard-linked files are not allowed: {rel}")


def _parse_scalar(raw: str, line_number: int) -> str:
    raw = raw.strip()
    if not raw:
        raise SkillError(f"line {line_number}: scalar value is empty")
    if raw.startswith('"'):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SkillError(f"line {line_number}: invalid quoted scalar") from exc
        if not isinstance(value, str):
            raise SkillError(f"line {line_number}: value must be a string")
        return value
    if raw.casefold() in YAML_RESERVED or not PLAIN_SCALAR_RE.fullmatch(raw):
        raise SkillError(f"line {line_number}: ambiguous plain scalar; use JSON double quotes")
    return raw


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise SkillError("SKILL.md must begin with --- frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise SkillError("SKILL.md frontmatter is not closed") from exc
    fields: dict[str, str] = {}
    for index, line in enumerate(lines[1:end], start=2):
        if not line or line[0].isspace() or ":" not in line:
            raise SkillError(f"line {index}: expected an unindented key: value")
        key, raw = line.split(":", 1)
        if key not in ALLOWED_FIELDS:
            raise SkillError(f"line {index}: unsupported frontmatter field {key!r}")
        if key in fields:
            raise SkillError(f"line {index}: duplicate field {key!r}")
        fields[key] = _parse_scalar(raw, index)
    body = "\n".join(lines[end + 1 :]).strip()
    if not body:
        raise SkillError("SKILL.md body must not be empty")
    return fields, body


def validate_skill(path: Path, expected_name: str | None = None) -> dict[str, str]:
    path = path.expanduser()
    try:
        root_stat = path.lstat()
    except OSError as exc:
        raise SkillError(f"skill directory does not exist: {path}") from exc
    if path.is_symlink() or portable_paths.is_windows_reparse_point(root_stat):
        raise SkillError("skill directory must not be a symbolic link or reparse point")
    path = path.resolve()
    if not path.is_dir():
        raise SkillError(f"skill directory does not exist: {path}")
    validate_name(expected_name or path.name)
    validate_tree_paths(path)
    skill_file = path / "SKILL.md"
    if not skill_file.is_file() or skill_file.is_symlink():
        raise SkillError("skill must contain a regular SKILL.md")
    try:
        text = skill_file.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SkillError("SKILL.md must be UTF-8") from exc
    fields, _ = parse_frontmatter(text)
    missing = {"name", "description"} - fields.keys()
    if missing:
        raise SkillError(f"missing required field(s): {', '.join(sorted(missing))}")
    validate_name(fields["name"])
    if fields["name"] != (expected_name or path.name):
        raise SkillError("name must match the parent directory name")
    if not (1 <= len(fields["description"]) <= 1024):
        raise SkillError("description must be 1-1024 characters")
    compatibility = fields.get("compatibility")
    if compatibility is not None and not (1 <= len(compatibility) <= 500):
        raise SkillError("compatibility must be 1-500 characters")
    return fields


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def create_skill(
    name: str, output: Path, description: str | None, resources: list[str]
) -> Path:
    validate_name(name)
    if description is None:
        label = name.replace("-", " ")
        description = f"Assist with {label}. Use when a request specifically concerns {label}."
    if not (1 <= len(description) <= 1024):
        raise SkillError("description must be 1-1024 characters")
    output = output.expanduser().resolve()
    target = output / name
    if (
        os.name == "nt"
        and portable_paths.utf16_units(str(target))
        > portable_paths.MAX_WINDOWS_ABSOLUTE_PATH_UTF16_UNITS
    ):
        raise SkillError(
            "Windows output path exceeds "
            f"{portable_paths.MAX_WINDOWS_ABSOLUTE_PATH_UTF16_UNITS} UTF-16 code units"
        )
    if target.exists():
        raise SkillError(f"destination already exists: {target}")
    output.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{name}.", dir=output))
    try:
        skill_md = (
            "---\n"
            f"name: {_quoted(name)}\n"
            f"description: {_quoted(description)}\n"
            "license: Apache-2.0\n"
            "---\n\n"
            f"# {name.replace('-', ' ').title()}\n\n"
            "Describe the outcome, essential constraints, and task-specific workflow.\n"
        )
        (stage / "SKILL.md").write_text(skill_md, encoding="utf-8")
        for resource in resources:
            (stage / resource).mkdir()
        validate_skill(stage, expected_name=name)
        os.replace(stage, target)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="skill-tool")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-skill", help="create a new skill")
    create.add_argument("name")
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--description")
    create.add_argument(
        "--resource",
        action="append",
        choices=("scripts", "references", "assets"),
        default=[],
    )
    validate = commands.add_parser("validate", help="validate a skill")
    validate.add_argument("path", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "create-skill":
            created = create_skill(args.name, args.output, args.description, args.resource)
            print(created)
        else:
            fields = validate_skill(args.path)
            print(f"valid: {fields['name']}")
        return 0
    except (OSError, SkillError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
