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

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import portable_paths

CANONICAL_SOURCE = "https://github.com/OkYongChoi/skills.git"
# Reviewed cross-platform release commit. A repository catalog ref is preferred when
# this script is run from a checkout containing a newer approved catalog.
CANONICAL_REF: str | None = "a43a268215d898bee9700b9bbb3c7d39bb3006f8"
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
DIGEST_ALGORITHM = "portable-tree-sha256-v2"


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
    try:
        catalog_stat = catalog_file.lstat()
    except OSError:
        return None
    if (
        catalog_file.is_symlink()
        or portable_paths.is_windows_reparse_point(catalog_stat)
        or not stat.S_ISREG(catalog_stat.st_mode)
    ):
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
        source_path = Path(source).expanduser()
        try:
            source_stat = source_path.lstat()
        except OSError as exc:
            raise InstallError(f"local source is not a directory: {source_path}") from exc
        if (
            source_path.is_symlink()
            or portable_paths.is_windows_reparse_point(source_stat)
            or not stat.S_ISDIR(source_stat.st_mode)
        ):
            raise InstallError(
                f"local source must not be a symbolic link or reparse point: {source_path}"
            )
        root = source_path.resolve()
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
    try:
        return portable_paths.validate_portable_relative_path(
            value, label="catalog path"
        )
    except portable_paths.PortablePathError as exc:
        raise InstallError(str(exc)) from exc


def _validate_name(name: object, *, label: str = "skill name") -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise InstallError(f"invalid {label}: {name!r}")
    try:
        portable_paths.validate_portable_component(name, label=label)
    except portable_paths.PortablePathError as exc:
        raise InstallError(str(exc)) from exc
    return name


def load_catalog(root: Path) -> dict[str, dict[str, object]]:
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise InstallError("catalog root must be a regular directory") from exc
    if (
        root.is_symlink()
        or portable_paths.is_windows_reparse_point(root_stat)
        or not stat.S_ISDIR(root_stat.st_mode)
    ):
        raise InstallError("catalog root must not be a symbolic link or reparse point")
    catalog_file = root / "catalog.json"
    try:
        catalog_stat = catalog_file.lstat()
    except OSError as exc:
        raise InstallError("catalog.json must be a regular file") from exc
    if (
        catalog_file.is_symlink()
        or portable_paths.is_windows_reparse_point(catalog_stat)
        or not stat.S_ISREG(catalog_stat.st_mode)
    ):
        raise InstallError("catalog.json must be a regular file")
    try:
        raw = json.loads(catalog_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"cannot read catalog.json: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("format_version") != 1:
        raise InstallError("unsupported catalog format")
    if raw.get("digest_algorithm") != DIGEST_ALGORITHM:
        raise InstallError(
            f"catalog digest_algorithm must be {DIGEST_ALGORITHM!r}"
        )
    items = raw.get("skills")
    if not isinstance(items, list):
        raise InstallError("catalog skills must be a list")
    catalog: dict[str, dict[str, object]] = {}
    seen_paths: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            raise InstallError("catalog entry must be an object")
        name = item.get("name")
        digest = item.get("sha256")
        name = _validate_name(name, label="catalog skill name")
        if name in catalog:
            raise InstallError(f"duplicate catalog skill: {name}")
        catalog_path = _safe_catalog_path(item.get("path"))
        path_key = portable_paths.portable_path_key(catalog_path)
        previous_path = seen_paths.setdefault(path_key, catalog_path.as_posix())
        if previous_path != catalog_path.as_posix():
            raise InstallError(
                f"portable-colliding catalog paths: {previous_path!r} and "
                f"{catalog_path.as_posix()!r}"
            )
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
    _validate_name(expected_name)
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
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise InstallError("skill path must be a regular directory") from exc
    if (
        root.is_symlink()
        or portable_paths.is_windows_reparse_point(root_stat)
        or not stat.S_ISDIR(root_stat.st_mode)
    ):
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
            try:
                pure = portable_paths.validate_portable_relative_path(
                    rel, label="skill path"
                )
            except portable_paths.PortablePathError as exc:
                raise InstallError(str(exc)) from exc
            folded = portable_paths.portable_path_key(pure)
            previous = seen_case.setdefault(folded, rel)
            if previous != rel:
                raise InstallError(
                    f"normalization/case-colliding paths: {previous!r} and {rel!r}"
                )
            info = entry.stat(follow_symlinks=False)
            mode = info.st_mode
            if stat.S_ISLNK(mode) or portable_paths.is_windows_reparse_point(info):
                raise InstallError(f"symbolic links and reparse points are not allowed: {rel}")
            if stat.S_ISDIR(mode):
                stack.append(path)
                continue
            if not stat.S_ISREG(mode):
                raise InstallError(f"special files are not allowed: {rel}")
            try:
                info = portable_paths.full_file_stat(path, info)
                if portable_paths.is_windows_reparse_point(info):
                    raise portable_paths.PortablePathError(
                        f"reparse points are not allowed: {rel}"
                    )
                link_count = portable_paths.file_link_count(path, info)
            except portable_paths.PortablePathError as exc:
                raise InstallError(str(exc)) from exc
            if link_count != 1:
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
    digest.update(DIGEST_ALGORITHM.encode("ascii") + b"\0")
    for path, rel, info in files:
        digest.update(b"file\0")
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(info.st_size).encode("ascii"))
        digest.update(b"\0")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise InstallError(f"cannot safely open skill file: {rel}") from exc
        opened = os.fstat(fd)
        identity = ("st_dev", "st_ino", "st_size", "st_mode")
        if any(getattr(opened, field) != getattr(info, field) for field in identity):
            os.close(fd)
            raise InstallError(f"skill file changed during verification: {rel}")
        try:
            opened_link_count = portable_paths.file_link_count(path, opened)
        except portable_paths.PortablePathError as exc:
            os.close(fd)
            raise InstallError(str(exc)) from exc
        if opened_link_count != 1:
            os.close(fd)
            raise InstallError(f"skill file changed during verification: {rel}")
        with os.fdopen(fd, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _resolve_skill_path(root: Path, item: dict[str, object]) -> Path:
    relative = _safe_catalog_path(item["path"])
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise InstallError("source root is not a regular directory") from exc
    if root.is_symlink() or portable_paths.is_windows_reparse_point(root_stat):
        raise InstallError("source root must not be a symbolic link or reparse point")
    root = root.resolve()
    candidate = root
    for part in relative.parts:
        candidate = candidate / part
        try:
            candidate_stat = candidate.lstat()
        except OSError as exc:
            raise InstallError(f"catalog path does not exist: {candidate}") from exc
        if candidate.is_symlink() or portable_paths.is_windows_reparse_point(candidate_stat):
            raise InstallError(
                f"catalog path contains a symbolic link or reparse point: {candidate}"
            )
    path = candidate.resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise InstallError("catalog path escapes source root") from exc
    return path


def _windows_pid_is_running(pid: int) -> bool:
    """Query a Windows process without sending a signal.

    Only ERROR_INVALID_PARAMETER is treated as a definitely missing process.
    Access denied and unexpected API failures fail closed as "running".
    """

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        process_query_limited_information = 0x1000
        handle = open_process(process_query_limited_information, False, pid)
        if handle:
            close_handle(handle)
            return True
        return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER
    except (AttributeError, OSError, ValueError):
        return True


def _pid_is_running(pid: int, platform: str | None = None) -> bool:
    if (platform or os.name) == "nt":
        return _windows_pid_is_running(pid)
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
    try:
        lock_link_count = portable_paths.file_link_count(lock, before)
    except portable_paths.PortablePathError:
        return False
    if (
        stale_after < 0
        or not stat.S_ISREG(before.st_mode)
        or portable_paths.is_windows_reparse_point(before)
        or lock_link_count != 1
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
    if (
        os.name == "nt"
        and portable_paths.utf16_units(str(target))
        > portable_paths.MAX_WINDOWS_ABSOLUTE_PATH_UTF16_UNITS
    ):
        raise InstallError(
            "Windows installation path exceeds "
            f"{portable_paths.MAX_WINDOWS_ABSOLUTE_PATH_UTF16_UNITS} UTF-16 code units"
        )
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
                try:
                    copied_link_count = portable_paths.file_link_count(output, copied)
                except portable_paths.PortablePathError as exc:
                    raise InstallError(str(exc)) from exc
                if (
                    not stat.S_ISREG(copied.st_mode)
                    or copied_link_count != 1
                    or portable_paths.is_windows_reparse_point(copied)
                ):
                    raise InstallError(f"source changed while copying: {rel}")
                # NTFS checkouts do not provide portable POSIX executable bits.
                # Preserve approved source modes on POSIX, while Windows uses a
                # writable regular-file mode and invokes scripts via Python.
                output_mode = (
                    0o644
                    if os.name == "nt"
                    else stat.S_IMODE(source_info.st_mode) & 0o755
                )
                os.chmod(output, output_mode)
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
