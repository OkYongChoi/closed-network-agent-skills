#!/usr/bin/env python3
"""Run dependency-free macOS/Linux/Windows and line-ending acceptance checks."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".md", ".py", ".json", ".yml", ".yaml", ".sh", ".ps1", ".cmd", ".bat"}


class VerifyError(RuntimeError):
    pass


def run(command: list[str], *, cwd: Path, capture: bool = False) -> str:
    print("+ " + " ".join(command), flush=True)
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
    except FileNotFoundError as exc:
        raise VerifyError(f"required executable not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise VerifyError(f"command timed out: {' '.join(command)}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise VerifyError(detail or f"command failed: {' '.join(command)}") from exc
    return result.stdout if capture else ""


def catalogued_text_paths(root: Path) -> list[str]:
    catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
    paths = ["catalog.json"]
    for item in catalog["skills"]:
        skill_root = root.joinpath(*PurePosixPath(item["path"]).parts)
        for path in skill_root.rglob("*"):
            if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
                paths.append(path.relative_to(root).as_posix())
    return sorted(set(paths))


def parse_check_attr_z(output: bytes) -> dict[str, dict[str, str]]:
    """Parse `git check-attr -z` path/attribute/value triples."""

    fields = output.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    if len(fields) % 3:
        raise VerifyError("git check-attr returned malformed NUL-delimited output")
    attributes: dict[str, dict[str, str]] = {}
    try:
        for index in range(0, len(fields), 3):
            path, attribute, value = (
                field.decode("utf-8") for field in fields[index : index + 3]
            )
            attributes.setdefault(path, {})[attribute] = value
    except UnicodeDecodeError as exc:
        raise VerifyError("git check-attr returned non-UTF-8 path metadata") from exc
    return attributes


def verify_git_attributes(root: Path) -> None:
    paths = catalogued_text_paths(root)
    payload = b"".join(path.encode("utf-8") + b"\0" for path in paths)
    print("+ git check-attr -z --stdin text eol", flush=True)
    result = subprocess.run(
        ["git", "check-attr", "-z", "--stdin", "text", "eol"],
        cwd=root,
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise VerifyError(detail or "git check-attr failed")
    attributes = parse_check_attr_z(result.stdout)
    failures = [
        path
        for path in paths
        if attributes.get(path, {}).get("text") not in {"set", "auto"}
        or attributes.get(path, {}).get("eol") != "lf"
    ]
    if failures:
        raise VerifyError(f"hashed text paths are not forced to LF: {failures}")
    binary = run(
        ["git", "check-attr", "text", "--", "__attribute_probe__.png"],
        cwd=root,
        capture=True,
    )
    if not binary.rstrip().endswith(": text: unset"):
        raise VerifyError("binary .gitattributes rule does not disable text conversion")


def run_repository_checks(root: Path) -> None:
    run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=root)
    run([sys.executable, "-B", "scripts/refresh_catalog.py", "--check"], cwd=root)
    verify_git_attributes(root)


def tracked_and_untracked_files(root: Path) -> list[str]:
    output = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
    )
    return [entry.decode("utf-8") for entry in output.split(b"\0") if entry]


def verify_autocrlf_clone(root: Path) -> None:
    """Commit a temporary snapshot, clone it with autocrlf=true, then verify."""

    with tempfile.TemporaryDirectory(prefix="skills-platform-") as temp:
        base = Path(temp)
        seed = base / "seed"
        clone = base / "clone"
        seed.mkdir()
        for relative in tracked_and_untracked_files(root):
            source = root.joinpath(*PurePosixPath(relative).parts)
            target = seed.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        binary_probe = b"\x89PNG\r\n\x1a\n\x00binary\r\ncontent\x00"
        (seed / "__attribute_probe__.png").write_bytes(binary_probe)
        run(["git", "init", "--quiet"], cwd=seed)
        run(["git", "config", "user.email", "platform-test@example.invalid"], cwd=seed)
        run(["git", "config", "user.name", "Platform Test"], cwd=seed)
        run(["git", "add", "--all"], cwd=seed)
        run(
            [
                "git",
                "-c",
                "commit.gpgSign=false",
                "commit",
                "--quiet",
                "-m",
                "platform fixture",
            ],
            cwd=seed,
        )
        run(
            [
                "git",
                "-c",
                "core.autocrlf=true",
                "-c",
                "core.eol=crlf",
                "clone",
                "--quiet",
                "--no-local",
                str(seed),
                str(clone),
            ],
            cwd=base,
        )
        if (clone / "__attribute_probe__.png").read_bytes() != binary_probe:
            raise VerifyError("binary content changed in core.autocrlf=true clone")
        run_repository_checks(clone)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--autocrlf-clone",
        action="store_true",
        help="also verify a temporary core.autocrlf=true clean clone",
    )
    args = parser.parse_args(argv)
    try:
        if sys.version_info < (3, 11):
            raise VerifyError("Python 3.11 or newer is required")
        os.environ.setdefault("PYTHONUTF8", "1")
        run_repository_checks(ROOT)
        if args.autocrlf_clone:
            verify_autocrlf_clone(ROOT)
        print(f"platform verification passed: {sys.platform}")
        return 0
    except (OSError, UnicodeError, json.JSONDecodeError, VerifyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
