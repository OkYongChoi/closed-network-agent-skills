#!/usr/bin/env python3
"""Offline-friendly, checksum-verifying Agent Skill installer.

Adapted from OpenAI's skill-installer. Modified to remove GitHub API and archive
downloads and to add portable catalogs and secure staged installation.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Iterator

CANONICAL_SOURCE = "https://github.com/OkYongChoi/skills.git"
# Reviewed initial release commit. A repository catalog ref is preferred when
# this script is run from a checkout containing a newer approved catalog.
CANONICAL_REF: str | None = "5eeb2a37a39d64daaeeac52d2467067bc34983ee"
FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ALLOWED_FIELDS = {"name", "description", "license", "compatibility"}
PLAIN_SCALAR_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._/-]*$")
YAML_RESERVED = {
    "null", "true", "false", "yes", "no", "on", "off", "y", "n", "~",
    ".nan", ".inf", "+.inf", "-.inf",
}
DEFAULT_MAX_FILES = 1_000
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_STALE_LOCK_SECONDS = 60 * 60
IGNORED_RUNTIME_DIRS = {"__pycache__"}
IGNORED_RUNTIME_FILES = {".DS_Store"}


class InstallError(RuntimeError):
    """An expected, user-facing installation failure."""


def _run_git(args: list[str], cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise InstallError("Git CLI was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise InstallError("Git operation timed out") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip().splitlines()
        message = detail[-1] if detail else "Git operation failed"
        raise InstallError(message) from exc
    return result.stdout.strip()


def _checkout_root() -> Path | None:
    candidate = Path(__file__).resolve().parents[3]
    if (candidate / "catalog.json").is_file() and (candidate / "skills").is_dir():
        return candidate
    return None


def select_source(cli_source: str | None) -> str:
    if cli_source:
        return cli_source
    if os.environ.get("AGENT_SKILLS_SOURCE"):
        return os.environ["AGENT_SKILLS_SOURCE"]
    checkout = _checkout_root()
    return str(checkout) if checkout else CANONICAL_SOURCE


def _catalog_repository_ref(root: Path | None) -> str | None:
    if root is None:
        return None
    catalog_file = root / "catalog.json"
    if catalog_file.is_symlink() or not catalog_file.is_file():
        return None
    try:
        raw = json.loads(catalog_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    repository = raw.get("repository") if isinstance(raw, dict) else None
    value = repository.get("ref") if isinstance(repository, dict) else None
    if value is None:
        return None
    if not isinstance(value, str) or not FULL_SHA_RE.fullmatch(value):
        raise InstallError("catalog repository.ref must be a full commit SHA or null")
    return value.lower()


def select_ref(cli_ref: str | None, source: str) -> str | None:
    """Select a ref in CLI, environment, then canonical catalog/constant order."""
    if cli_ref:
        return cli_ref
    if os.environ.get("AGENT_SKILLS_REF"):
        return os.environ["AGENT_SKILLS_REF"]
    if source == CANONICAL_SOURCE:
        return _catalog_repository_ref(_checkout_root()) or CANONICAL_REF
    return None


def _is_local_source(source: str) -> bool:
    return Path(source).expanduser().exists()


@contextlib.contextmanager
def acquire_source(
    source: str, ref: str | None, allow_mutable_ref: bool
) -> Iterator[tuple[Path, str | None]]:
    if _is_local_source(source):
        root = Path(source).expanduser().resolve()
        if not root.is_dir():
            raise InstallError(f"local source is not a directory: {root}")
        yield root, None
        return

    requested_ref = ref
    if not requested_ref:
        raise InstallError(
            "remote sources require --ref FULL_COMMIT_SHA or AGENT_SKILLS_REF"
        )
    if not FULL_SHA_RE.fullmatch(requested_ref) and not allow_mutable_ref:
        raise InstallError(
            "remote ref must be a full 40-character commit SHA; "
            "use --allow-mutable-ref only for development"
        )
    with tempfile.TemporaryDirectory(prefix="skills-source-") as temp:
        root = Path(temp) / "repo"
        root.mkdir()
        _run_git(["init", "--quiet"], root)
        _run_git(["remote", "add", "origin", source], root)
        _run_git(["fetch", "--quiet", "--depth=1", "origin", requested_ref], root)
        resolved = _run_git(["rev-parse", "FETCH_HEAD^{commit}"], root)
        if FULL_SHA_RE.fullmatch(requested_ref) and resolved.lower() != requested_ref.lower():
            raise InstallError("fetched commit does not match the requested SHA")
        _run_git(["checkout", "--quiet", "--detach", resolved], root)
        yield root, resolved


def _safe_catalog_path(value: object) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise InstallError("catalog path must be a non-empty string")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise InstallError(f"unsafe catalog path: {value!r}")
    return path


def load_catalog(root: Path) -> dict[str, dict[str, object]]:
    catalog_file = root / "catalog.json"
    if catalog_file.is_symlink() or not catalog_file.is_file():
        raise InstallError("catalog.json must be a regular file")
    try:
        raw = json.loads(catalog_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"cannot read catalog.json: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("format_version") != 1:
        raise InstallError("unsupported catalog format")
    items = raw.get("skills")
    if not isinstance(items, list):
        raise InstallError("catalog skills must be a list")
    catalog: dict[str, dict[str, object]] = {}
    for item in items:
        if not isinstance(item, dict):
            raise InstallError("catalog entry must be an object")
        name = item.get("name")
        digest = item.get("sha256")
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise InstallError(f"invalid catalog skill name: {name!r}")
        if name in catalog:
            raise InstallError(f"duplicate catalog skill: {name}")
        _safe_catalog_path(item.get("path"))
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise InstallError(f"invalid catalog digest for {name}")
        catalog[name] = item
    return catalog


def _parse_scalar(raw: str, line_number: int) -> str:
    raw = raw.strip()
    if not raw:
        raise InstallError(f"SKILL.md line {line_number}: scalar is empty")
    if raw.startswith('"'):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise InstallError(f"SKILL.md line {line_number}: invalid quoted scalar") from exc
        if not isinstance(value, str):
            raise InstallError(f"SKILL.md line {line_number}: value must be a string")
        return value
    if raw.casefold() in YAML_RESERVED or not PLAIN_SCALAR_RE.fullmatch(raw):
        raise InstallError(
            f"SKILL.md line {line_number}: ambiguous plain scalar; use JSON double quotes"
        )
    return raw


def validate_skill(skill_dir: Path, expected_name: str) -> None:
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.is_file() or skill_file.is_symlink():
        raise InstallError("skill must contain a regular SKILL.md")
    try:
        lines = skill_file.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise InstallError("SKILL.md must be UTF-8") from exc
    if not lines or lines[0] != "---":
        raise InstallError("SKILL.md must begin with frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise InstallError("SKILL.md frontmatter is not closed") from exc
    fields: dict[str, str] = {}
    for index, line in enumerate(lines[1:end], start=2):
        if not line or line[0].isspace() or ":" not in line:
            raise InstallError(f"SKILL.md line {index}: expected key: value")
        key, raw = line.split(":", 1)
        if key not in ALLOWED_FIELDS or key in fields:
            raise InstallError(f"SKILL.md line {index}: unsupported or duplicate field")
        fields[key] = _parse_scalar(raw, index)
    if fields.get("name") != expected_name:
        raise InstallError("SKILL.md name does not match catalog name")
    description = fields.get("description", "")
    if not (1 <= len(description) <= 1024):
        raise InstallError("SKILL.md description must be 1-1024 characters")
    if not "\n".join(lines[end + 1 :]).strip():
        raise InstallError("SKILL.md body must not be empty")


def inspect_tree(
    root: Path, max_files: int = DEFAULT_MAX_FILES, max_bytes: int = DEFAULT_MAX_BYTES
) -> list[tuple[Path, str, os.stat_result]]:
    if root.is_symlink() or not root.is_dir():
        raise InstallError("skill path must be a regular directory")
    files: list[tuple[Path, str, os.stat_result]] = []
    seen_case: dict[str, str] = {}
    total = 0
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise InstallError(f"cannot scan skill tree: {exc}") from exc
        for entry in entries:
            path = Path(entry.path)
            rel = path.relative_to(root).as_posix()
            if (
                entry.name in IGNORED_RUNTIME_DIRS
                or entry.name in IGNORED_RUNTIME_FILES
                or entry.name.endswith((".pyc", ".pyo"))
            ):
                continue
            pure = PurePosixPath(rel)
            if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
                raise InstallError(f"unsafe path in skill: {rel!r}")
            folded = rel.casefold()
            previous = seen_case.setdefault(folded, rel)
            if previous != rel:
                raise InstallError(f"case-colliding paths: {previous!r} and {rel!r}")
            info = entry.stat(follow_symlinks=False)
            mode = info.st_mode
            if stat.S_ISLNK(mode):
                raise InstallError(f"symbolic links are not allowed: {rel}")
            if stat.S_ISDIR(mode):
                stack.append(path)
                continue
            if not stat.S_ISREG(mode):
                raise InstallError(f"special files are not allowed: {rel}")
            if info.st_nlink != 1:
                raise InstallError(f"hard-linked files are not allowed: {rel}")
            files.append((path, rel, info))
            total += info.st_size
            if len(files) > max_files:
                raise InstallError(f"skill exceeds file limit ({max_files})")
            if total > max_bytes:
                raise InstallError(f"skill exceeds byte limit ({max_bytes})")
    return sorted(files, key=lambda item: item[1].encode("utf-8"))


def tree_digest(files: list[tuple[Path, str, os.stat_result]]) -> str:
    digest = hashlib.sha256()
    for path, rel, info in files:
        digest.update(b"file\0")
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(info.st_size).encode("ascii"))
        digest.update(b"\0")
        normalized_mode = stat.S_IMODE(info.st_mode) & 0o755
        digest.update(f"{normalized_mode:04o}".encode("ascii"))
        digest.update(b"\0")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise InstallError(f"cannot safely open skill file: {rel}") from exc
        opened = os.fstat(fd)
        identity = ("st_dev", "st_ino", "st_size", "st_mode", "st_nlink")
        if any(getattr(opened, field) != getattr(info, field) for field in identity):
            os.close(fd)
            raise InstallError(f"skill file changed during verification: {rel}")
        with os.fdopen(fd, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _resolve_skill_path(root: Path, item: dict[str, object]) -> Path:
    relative = _safe_catalog_path(item["path"])
    root = root.resolve()
    candidate = root
    for part in relative.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise InstallError(f"catalog path contains a symbolic link: {candidate}")
    path = candidate.resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise InstallError("catalog path escapes source root") from exc
    return path


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _recover_stale_lock(lock: Path, stale_after: int) -> bool:
    """Remove only an old, same-host lock whose recorded process is gone."""
    try:
        before = lock.lstat()
    except FileNotFoundError:
        return True
    if (
        stale_after < 0
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or time.time() - before.st_mtime < stale_after
    ):
        return False
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lock, flags)
    except OSError:
        return False
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            return False
        raw = os.read(fd, 4097)
    finally:
        os.close(fd)
    if len(raw) > 4096:
        return False
    try:
        metadata = json.loads(raw.decode("utf-8"))
        pid = metadata["pid"]
        host = metadata["host"]
        created = metadata["created"]
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if (
        not isinstance(pid, int)
        or pid < 1
        or host != socket.gethostname()
        or not isinstance(created, (int, float))
        or time.time() - created < stale_after
        or _pid_is_running(pid)
    ):
        return False
    try:
        current = lock.lstat()
    except FileNotFoundError:
        return True
    identity = ("st_dev", "st_ino", "st_mtime_ns", "st_size")
    if any(getattr(current, field) != getattr(before, field) for field in identity):
        return False
    try:
        lock.unlink()
    except FileNotFoundError:
        pass
    return True


@contextlib.contextmanager
def acquire_install_lock(lock: Path, stale_after: int) -> Iterator[None]:
    owned_identity: tuple[int, int] | None = None
    for attempt in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            if attempt == 0 and _recover_stale_lock(lock, stale_after):
                continue
            raise InstallError(f"another installation is in progress: {lock.name}") from exc
        info = os.fstat(fd)
        created_identity = (info.st_dev, info.st_ino)
        try:
            metadata = json.dumps(
                {"pid": os.getpid(), "host": socket.gethostname(), "created": time.time()},
                separators=(",", ":"),
            ).encode("utf-8")
            view = memoryview(metadata)
            while view:
                written = os.write(fd, view)
                if written < 1:
                    raise OSError("short write while creating install lock")
                view = view[written:]
            os.fsync(fd)
            owned_identity = created_identity
        except Exception:
            os.close(fd)
            try:
                current = lock.lstat()
                if created_identity == (current.st_dev, current.st_ino):
                    lock.unlink()
            except FileNotFoundError:
                pass
            raise
        else:
            os.close(fd)
        break
    else:  # pragma: no cover - the loop either acquires or raises
        raise InstallError(f"could not acquire installation lock: {lock.name}")
    try:
        yield
    finally:
        try:
            current = lock.lstat()
            if owned_identity == (current.st_dev, current.st_ino):
                lock.unlink()
        except FileNotFoundError:
            pass


def install_skill(
    root: Path,
    item: dict[str, object],
    destination: Path,
    max_files: int,
    max_bytes: int,
    stale_lock_seconds: int = DEFAULT_STALE_LOCK_SECONDS,
) -> Path:
    name = str(item["name"])
    source = _resolve_skill_path(root, item)
    files = inspect_tree(source, max_files, max_bytes)
    validate_skill(source, name)
    actual = tree_digest(files)
    if actual != item["sha256"]:
        raise InstallError(
            f"checksum mismatch for {name}: expected {item['sha256']}, got {actual}"
        )
    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / name
    lock = destination / f".{name}.install.lock"
    with acquire_install_lock(lock, stale_lock_seconds):
        if target.exists() or target.is_symlink():
            raise InstallError(f"destination already exists: {target}")
        stage_root = Path(tempfile.mkdtemp(prefix=f".{name}.stage-", dir=destination))
        staged = stage_root / name
        try:
            staged.mkdir()
            for source_file, rel, source_info in files:
                output = staged.joinpath(*PurePosixPath(rel).parts)
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_file, output, follow_symlinks=False)
                copied = output.lstat()
                if not stat.S_ISREG(copied.st_mode) or copied.st_nlink != 1:
                    raise InstallError(f"source changed while copying: {rel}")
                os.chmod(output, stat.S_IMODE(source_info.st_mode) & 0o755)
            staged_files = inspect_tree(staged, max_files, max_bytes)
            validate_skill(staged, name)
            if tree_digest(staged_files) != actual:
                raise InstallError("staged copy failed integrity verification")
            os.replace(staged, target)
            return target
        finally:
            shutil.rmtree(stage_root, ignore_errors=True)


def _destination(args: argparse.Namespace) -> Path:
    if args.dest:
        return args.dest
    agent_home = args.agent_home or Path(
        os.environ.get("AGENT_HOME", str(Path.home() / ".agents"))
    )
    return agent_home / "skills"


def _add_source_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source")
    parser.add_argument("--ref")
    parser.add_argument("--allow-mutable-ref", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="skill-installer")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="list catalogued skills")
    _add_source_options(listing)
    install = commands.add_parser("install", help="install one catalogued skill")
    install.add_argument("name")
    _add_source_options(install)
    install.add_argument("--dest", type=Path)
    install.add_argument("--agent-home", type=Path)
    install.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    install.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    install.add_argument(
        "--stale-lock-seconds", type=int, default=DEFAULT_STALE_LOCK_SECONDS
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        source = select_source(args.source)
        selected_ref = select_ref(args.ref, source)
        with acquire_source(source, selected_ref, args.allow_mutable_ref) as (root, commit):
            catalog = load_catalog(root)
            if args.command == "list":
                for name in sorted(catalog):
                    description = catalog[name].get("description", "")
                    print(f"{name}\t{description}")
                if commit:
                    print(f"source-commit\t{commit}", file=sys.stderr)
            else:
                if args.name not in catalog:
                    raise InstallError(f"skill is not in catalog: {args.name}")
                if args.max_files < 1 or args.max_bytes < 1 or args.stale_lock_seconds < 0:
                    raise InstallError("size limits must be positive and stale lock age non-negative")
                installed = install_skill(
                    root,
                    catalog[args.name],
                    _destination(args),
                    args.max_files,
                    args.max_bytes,
                    args.stale_lock_seconds,
                )
                print(installed)
        return 0
    except (InstallError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
