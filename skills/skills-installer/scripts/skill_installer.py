#!/usr/bin/env python3
"""Offline-friendly, checksum-verifying Agent Skill installer.

Adapted from OpenAI's skill-installer. Modified to remove GitHub API and archive
downloads and to add portable catalogs and secure staged installation.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
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
from typing import Iterator, NamedTuple
from urllib.parse import urlsplit, urlunsplit

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import portable_paths

CANONICAL_SOURCE = "https://github.com/OkYongChoi/closed-network-agent-skills.git"
# Reviewed cross-platform release commit. A repository catalog ref is preferred when
# this script is run from a checkout containing a newer approved catalog.
CANONICAL_REF: str | None = "a8257ac34b9f9ca9af2658abbefe5068f24f4060"
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
MAX_CONFIG_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
APPROVED_REF = "refs/heads/latest-approved"
RELEASE_MANIFEST_PATH = "release-manifest.json"
INSTALL_METADATA_DIR = ".agent-install-metadata"
CONFIG_TOP_LEVEL_FIELDS = {"skills", "plugins", "agentHome"}
SKILLS_CONFIG_FIELDS = {"source", "ref", "allowMutableRef"}
PLUGINS_CONFIG_FIELDS = {"source", "ref", "allowMutableRef", "defaultTarget"}
AUTO_CHECKOUT = object()


class InstallError(RuntimeError):
    """An expected, user-facing installation failure."""


class EffectiveConfig(NamedTuple):
    """Resolved installer settings and their provenance."""

    source: str
    ref: str | None
    allow_mutable_ref: bool
    agent_home: Path
    origins: dict[str, str]


class ApprovedRelease(NamedTuple):
    source: str
    ref: str | None
    version: str | None
    updated_at: str | None
    digests: dict[str, str]
    catalog_sha256: str | None
    pointer_commit: str | None
    sequence: int | None


def _nonempty_string(value: object, *, field: str, path: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InstallError(f"{path}: {field} must be a non-empty string")
    if "\x00" in value:
        raise InstallError(f"{path}: {field} must not contain NUL")
    return value


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _validate_config_section(
    raw: object, *, name: str, allowed: set[str], path: Path
) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise InstallError(f"{path}: {name} must be an object")
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise InstallError(f"{path}: unknown {name} field: {unknown[0]}")
    for field in ("source", "ref"):
        if field in raw:
            _nonempty_string(raw[field], field=f"{name}.{field}", path=path)
    if "allowMutableRef" in raw and not isinstance(raw["allowMutableRef"], bool):
        raise InstallError(f"{path}: {name}.allowMutableRef must be a boolean")
    if "defaultTarget" in raw:
        target = _nonempty_string(
            raw["defaultTarget"], field=f"{name}.defaultTarget", path=path
        )
        if target not in {"portable", "codex", "claude"}:
            raise InstallError(
                f"{path}: {name}.defaultTarget must be portable, codex, or claude"
            )
    return raw


def load_config(path: Path) -> dict[str, object] | None:
    """Load one strict JSON config. Missing files are the only files skipped."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InstallError(f"cannot inspect config {path}: {exc}") from exc
    if (
        path.is_symlink()
        or portable_paths.is_windows_reparse_point(info)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
    ):
        raise InstallError(f"config must be a regular, singly linked file: {path}")
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino)
                or portable_paths.is_windows_reparse_point(opened)
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
            ):
                raise InstallError(f"config changed while opening: {path}")
            encoded = stream.read(MAX_CONFIG_BYTES + 1)
        if len(encoded) > MAX_CONFIG_BYTES:
            raise InstallError(f"config exceeds {MAX_CONFIG_BYTES} bytes: {path}")
        raw = json.loads(encoded.decode("utf-8"), object_pairs_hook=_json_object)
    except InstallError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise InstallError(f"cannot parse JSON config {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise InstallError(f"{path}: top-level config must be an object")
    unknown = sorted(set(raw) - CONFIG_TOP_LEVEL_FIELDS)
    if unknown:
        raise InstallError(f"{path}: unknown top-level field: {unknown[0]}")
    if "skills" in raw:
        _validate_config_section(
            raw["skills"], name="skills", allowed=SKILLS_CONFIG_FIELDS, path=path
        )
    if "plugins" in raw:
        _validate_config_section(
            raw["plugins"], name="plugins", allowed=PLUGINS_CONFIG_FIELDS, path=path
        )
    if "agentHome" in raw:
        _nonempty_string(raw["agentHome"], field="agentHome", path=path)
    return raw


def config_paths(
    *,
    environ: dict[str, str] | os._Environ[str] | None = None,
    os_name: str | None = None,
    home: Path | None = None,
) -> tuple[Path, Path]:
    """Return user/system config paths with injectable platform inputs for tests."""
    env = os.environ if environ is None else environ
    platform = os.name if os_name is None else os_name
    fallback_home = Path.home() if home is None else home
    if platform == "nt":
        user_root = Path(env.get("USERPROFILE", str(fallback_home)))
        system_root = Path(env.get("ProgramData", r"C:\ProgramData"))
        return (
            user_root / ".agents" / "config.json",
            system_root / "AgentTools" / "config.json",
        )
    return (
        fallback_home / ".agents" / "config.json",
        Path("/etc/agent-tools/config.json"),
    )


def _expand_home(value: str, home: Path) -> Path:
    if value == "~":
        return home
    if value.startswith("~/") or value.startswith("~\\"):
        return home / value[2:]
    if value.startswith("~"):
        raise InstallError("agentHome only supports the current user's ~ prefix")
    return Path(value)


def resolve_effective_config(
    args: argparse.Namespace,
    *,
    environ: dict[str, str] | os._Environ[str] | None = None,
    os_name: str | None = None,
    home: Path | None = None,
    checkout: Path | None | object = AUTO_CHECKOUT,
    user_config_path: Path | None = None,
    system_config_path: Path | None = None,
) -> EffectiveConfig:
    """Resolve settings per field in CLI, env, user, system, checkout order."""
    env = os.environ if environ is None else environ
    effective_home = Path.home() if home is None else home
    default_user, default_system = config_paths(
        environ=env, os_name=os_name, home=effective_home
    )
    user_path = default_user if user_config_path is None else user_config_path
    system_path = default_system if system_config_path is None else system_config_path

    # Always parse both present files so a malformed lower-priority file cannot hide.
    user = load_config(user_path) or {}
    system = load_config(system_path) or {}
    user_skills = user.get("skills", {})
    system_skills = system.get("skills", {})
    assert isinstance(user_skills, dict) and isinstance(system_skills, dict)

    origins: dict[str, str] = {}

    def choose(field: str, cli: object, env_name: str | None) -> object | None:
        if cli is not None:
            origins[field] = "cli"
            return cli
        if env_name and env.get(env_name):
            origins[field] = f"env:{env_name}"
            return env[env_name]
        if field in user_skills:
            origins[field] = f"user-config:{user_path}"
            return user_skills[field]
        if field in system_skills:
            origins[field] = f"system-config:{system_path}"
            return system_skills[field]
        return None

    source_value = choose(
        "source", getattr(args, "source", None), "AGENT_SKILLS_SOURCE"
    )
    if source_value is None:
        selected_checkout = (
            _checkout_root() if checkout is AUTO_CHECKOUT else checkout
        )
        if selected_checkout is not None:
            source = str(selected_checkout)
            origins["source"] = "current-checkout"
        else:
            source = CANONICAL_SOURCE
            origins["source"] = "embedded-fallback"
    else:
        source = str(source_value)
    if not source or source != source.strip() or "\x00" in source:
        raise InstallError(
            "effective source must be a non-empty string without surrounding whitespace"
        )

    ref_value = choose("ref", getattr(args, "ref", None), "AGENT_SKILLS_REF")
    if ref_value is None and origins.get("source") == "embedded-fallback":
        selected_checkout = (
            _checkout_root() if checkout is AUTO_CHECKOUT else checkout
        )
        catalog_ref = _catalog_repository_ref(selected_checkout)
        ref_value = catalog_ref or CANONICAL_REF
        origins["ref"] = (
            "checkout-catalog" if catalog_ref else "embedded-fallback"
        )
    elif ref_value is None:
        origins["ref"] = "not-required-local" if _is_local_source(source) else "unset"
    # Branch and tag names are case-sensitive. Preserve the configured spelling;
    # full commit SHA comparisons already normalize case at the comparison site.
    ref = str(ref_value) if ref_value is not None else None
    if ref is not None and (not ref or ref != ref.strip() or "\x00" in ref):
        raise InstallError(
            "effective ref must be a non-empty string without surrounding whitespace"
        )

    mutable_cli = getattr(args, "allow_mutable_ref", None)
    mutable_value = choose("allowMutableRef", mutable_cli, None)
    allow_mutable = bool(mutable_value) if mutable_value is not None else False
    if mutable_value is None:
        origins["allowMutableRef"] = "secure-default"

    cli_home = getattr(args, "agent_home", None)
    if cli_home is not None:
        agent_home = Path(cli_home)
        origins["agentHome"] = "cli"
    elif "agentHome" in user:
        agent_home = _expand_home(str(user["agentHome"]), effective_home)
        origins["agentHome"] = f"user-config:{user_path}"
    elif "agentHome" in system:
        agent_home = _expand_home(str(system["agentHome"]), effective_home)
        origins["agentHome"] = f"system-config:{system_path}"
    elif env.get("AGENT_HOME"):
        # Backward-compatible, lower-priority fallback; central JSON takes precedence.
        agent_home = Path(env["AGENT_HOME"])
        origins["agentHome"] = "legacy-env:AGENT_HOME"
    else:
        agent_home = effective_home / ".agents"
        origins["agentHome"] = "platform-default"

    if (
        not _is_local_source(source)
        and ref is not None
        and not FULL_SHA_RE.fullmatch(ref)
        and not allow_mutable
    ):
        raise InstallError(
            "effective remote ref must be a full 40-character commit SHA; "
            "set allowMutableRef=true or pass --allow-mutable-ref only for development"
        )
    return EffectiveConfig(source, ref, allow_mutable, agent_home, origins)


def _redact_source(source: str) -> str:
    try:
        parsed = urlsplit(source)
        if not parsed.scheme or not parsed.netloc:
            return source
        hostname = parsed.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        userinfo = (
            "***@"
            if parsed.username is not None or parsed.password is not None
            else ""
        )
        return urlunsplit(
            (parsed.scheme, f"{userinfo}{hostname}{port}", parsed.path, "", "")
        )
    except ValueError:
        return "<invalid-url>"


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


def _source_has_credentials(source: str) -> bool:
    try:
        parsed = urlsplit(source)
        return (
            parsed.username is not None
            or parsed.password is not None
            or bool(parsed.query)
            or bool(parsed.fragment)
        )
    except ValueError:
        return True


def _parse_release_manifest(
    encoded: bytes, *, expected_source: str, allow_local_alias: bool = False
) -> ApprovedRelease:
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise InstallError(f"release manifest exceeds {MAX_MANIFEST_BYTES} bytes")
    try:
        raw = json.loads(encoded.decode("utf-8"), object_pairs_hook=_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise InstallError(f"cannot parse release manifest: {exc}") from exc
    if not isinstance(raw, dict):
        raise InstallError("release manifest must be an object")
    allowed = {
        "formatVersion", "kind", "name", "source", "ref", "version", "updatedAt",
        "catalogSha256", "packages", "sequence"
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise InstallError(f"unknown release manifest field: {unknown[0]}")
    if raw.get("formatVersion") != 1 or raw.get("kind") != "agent-skills-release":
        raise InstallError("unsupported release manifest format")
    if raw.get("name") != "skills":
        raise InstallError("release manifest name must be 'skills'")
    sequence = raw.get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise InstallError("release manifest sequence must be a non-negative integer")
    source = _nonempty_string(
        raw.get("source"), field="release manifest source", path=Path(RELEASE_MANIFEST_PATH)
    )
    if _source_has_credentials(source):
        raise InstallError("release manifest source must not contain credentials")
    if source != expected_source and not allow_local_alias:
        raise InstallError("release manifest source does not match the configured source")
    ref = _nonempty_string(
        raw.get("ref"), field="release manifest ref", path=Path(RELEASE_MANIFEST_PATH)
    )
    if not FULL_SHA_RE.fullmatch(ref):
        raise InstallError("release manifest ref must be a full 40-character commit SHA")
    version = _nonempty_string(
        raw.get("version"), field="release manifest version", path=Path(RELEASE_MANIFEST_PATH)
    )
    updated_at = _nonempty_string(
        raw.get("updatedAt"), field="release manifest updatedAt", path=Path(RELEASE_MANIFEST_PATH)
    )
    # Require an unambiguous UTC RFC 3339 timestamp without depending on third-party parsers.
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", updated_at):
        raise InstallError("release manifest updatedAt must be UTC RFC 3339 (YYYY-MM-DDTHH:MM:SSZ)")
    catalog_sha256 = raw.get("catalogSha256")
    if not isinstance(catalog_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", catalog_sha256
    ):
        raise InstallError("release manifest catalogSha256 must be lowercase SHA-256")
    artifacts = raw.get("packages")
    if not isinstance(artifacts, list) or not artifacts:
        raise InstallError("release manifest packages must be a non-empty list")
    digests: dict[str, str] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict) or set(artifact) != {"name", "digest"}:
            raise InstallError("release manifest package must contain only name and digest")
        name = _validate_name(artifact.get("name"), label="release artifact name")
        digest = artifact.get("digest")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise InstallError(f"invalid release artifact digest for {name}")
        if name in digests:
            raise InstallError(f"duplicate release artifact: {name}")
        digests[name] = digest
    return ApprovedRelease(
        source, ref.lower(), version, updated_at, digests, catalog_sha256, None, sequence
    )


def fetch_approved_release(source: str) -> ApprovedRelease:
    """Read the protected mutable pointer, returning only its immutable payload."""
    with tempfile.TemporaryDirectory(prefix="skills-pointer-") as temp:
        root = Path(temp)
        _run_git(["init", "--quiet"], root)
        _run_git(["remote", "add", "origin", source], root)
        _run_git(["fetch", "--quiet", "--depth=1", "origin", APPROVED_REF], root)
        pointer_commit = _run_git(["rev-parse", "FETCH_HEAD^{commit}"], root)
        try:
            manifest_size = int(
                _run_git(
                    ["cat-file", "-s", f"{pointer_commit}:{RELEASE_MANIFEST_PATH}"],
                    root,
                )
            )
        except ValueError as exc:
            raise InstallError("release manifest has an invalid Git object size") from exc
        if manifest_size > MAX_MANIFEST_BYTES:
            raise InstallError(
                f"release manifest exceeds {MAX_MANIFEST_BYTES} bytes"
            )
        text = _run_git(["show", f"{pointer_commit}:{RELEASE_MANIFEST_PATH}"], root)
    release = _parse_release_manifest(
        text.encode("utf-8"),
        expected_source=source,
        allow_local_alias=_is_local_source(source),
    )
    return release._replace(pointer_commit=pointer_commit)


def validate_release_catalog(
    release: ApprovedRelease,
    catalog: dict[str, dict[str, object]],
    catalog_file: Path,
) -> None:
    try:
        encoded = catalog_file.read_bytes()
    except OSError as exc:
        raise InstallError(f"cannot read approved catalog: {exc}") from exc
    if hashlib.sha256(encoded).hexdigest() != release.catalog_sha256:
        raise InstallError("release manifest catalogSha256 does not match catalog.json")
    actual = {name: str(item["sha256"]) for name, item in catalog.items()}
    if release.digests != actual:
        raise InstallError("release manifest packages do not match the approved catalog")


def _checkout_root() -> Path | None:
    candidate = Path(__file__).resolve().parents[3]
    if (candidate / "catalog.json").is_file() and (candidate / "skills").is_dir():
        return candidate
    return None


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


def _is_local_source(source: str) -> bool:
    return Path(source).expanduser().exists()


def _local_has_approved_pointer(source: str) -> bool:
    if not _is_local_source(source):
        return False
    path = Path(source).expanduser()
    if (path / "HEAD").is_file() and (path / "objects").is_dir():
        return True
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "show-ref", "--verify", "--quiet", APPROVED_REF],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


@contextlib.contextmanager
def acquire_source(
    source: str, ref: str | None, allow_mutable_ref: bool
) -> Iterator[tuple[Path, str | None]]:
    if _is_local_source(source) and ref is None:
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
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        )
        get_exit_code.restype = wintypes.BOOL
        process_query_limited_information = 0x1000
        handle = open_process(process_query_limited_information, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER
        try:
            exit_code = wintypes.DWORD()
            if not get_exit_code(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            close_handle(handle)
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
    *,
    replace: bool = False,
    provenance: dict[str, object] | None = None,
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
        target_exists = target.exists() or target.is_symlink()
        if target_exists and not replace:
            raise InstallError(f"destination already exists: {target}")
        if target_exists:
            target_info = target.lstat()
            if (
                target.is_symlink()
                or portable_paths.is_windows_reparse_point(target_info)
                or not stat.S_ISDIR(target_info.st_mode)
            ):
                raise InstallError(f"existing destination is not a regular directory: {target}")
        stage_root = Path(tempfile.mkdtemp(prefix=f".{name}.stage-", dir=destination))
        staged = stage_root / name
        backup = stage_root / "previous"
        metadata_temp: Path | None = None
        metadata_target: Path | None = None
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
            if provenance is not None:
                metadata_dir = destination / INSTALL_METADATA_DIR
                if metadata_dir.exists() or metadata_dir.is_symlink():
                    metadata_info = metadata_dir.lstat()
                    if (
                        metadata_dir.is_symlink()
                        or portable_paths.is_windows_reparse_point(metadata_info)
                        or not stat.S_ISDIR(metadata_info.st_mode)
                    ):
                        raise InstallError("install metadata path must be a regular directory")
                else:
                    metadata_dir.mkdir(mode=0o700)
                metadata_target = metadata_dir / f"{name}.json"
                if metadata_target.exists() or metadata_target.is_symlink():
                    metadata_info = metadata_target.lstat()
                    try:
                        metadata_links = portable_paths.file_link_count(
                            metadata_target, metadata_info
                        )
                    except portable_paths.PortablePathError as exc:
                        raise InstallError(str(exc)) from exc
                    if (
                        metadata_target.is_symlink()
                        or portable_paths.is_windows_reparse_point(metadata_info)
                        or not stat.S_ISREG(metadata_info.st_mode)
                        or metadata_links != 1
                    ):
                        raise InstallError("install metadata must be a regular, singly linked file")
                metadata_temp = metadata_dir / f".{name}.{os.getpid()}.tmp"
                if metadata_temp.exists() or metadata_temp.is_symlink():
                    raise InstallError("temporary install metadata path already exists")
                payload = dict(provenance)
                payload.update(
                    {
                        "formatVersion": 1,
                        "kind": "agent-skill-install",
                        "name": name,
                        "digest": actual,
                        "installedAt": datetime.datetime.now(
                            datetime.timezone.utc
                        ).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                    }
                )
                metadata_temp.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            new_moved = False
            metadata_backup = stage_root / "previous-metadata.json"
            try:
                if target_exists:
                    os.replace(target, backup)
                os.replace(staged, target)
                new_moved = True
                if metadata_temp is not None and metadata_target is not None:
                    if metadata_target.exists():
                        os.replace(metadata_target, metadata_backup)
                    os.replace(metadata_temp, metadata_target)
            # KeyboardInterrupt/SystemExit are still in-process failures. Restore
            # the previous installation before propagating them; only an
            # uncatchable process/host crash can interrupt this recovery path.
            except BaseException:
                if metadata_target is not None and metadata_backup.exists():
                    if metadata_target.exists():
                        os.replace(
                            metadata_target, stage_root / "failed-new-metadata.json"
                        )
                    os.replace(metadata_backup, metadata_target)
                elif (
                    metadata_temp is not None
                    and metadata_target is not None
                    and not metadata_temp.exists()
                    and metadata_target.exists()
                ):
                    os.replace(
                        metadata_target, stage_root / "failed-new-metadata.json"
                    )
                if backup.exists():
                    if target.exists():
                        os.replace(target, stage_root / "failed-new")
                    os.replace(backup, target)
                elif (new_moved or not staged.exists()) and target.exists():
                    failed = stage_root / "failed-new"
                    os.replace(target, failed)
                raise
            return target
        finally:
            if metadata_temp is not None:
                try:
                    metadata_temp.unlink()
                except FileNotFoundError:
                    pass
            shutil.rmtree(stage_root, ignore_errors=True)


def load_install_metadata(destination: Path, name: str) -> dict[str, object] | None:
    path = destination.expanduser().resolve() / INSTALL_METADATA_DIR / f"{name}.json"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InstallError(f"cannot inspect install metadata: {exc}") from exc
    try:
        links = portable_paths.file_link_count(path, info)
    except portable_paths.PortablePathError as exc:
        raise InstallError(str(exc)) from exc
    if (
        path.is_symlink()
        or portable_paths.is_windows_reparse_point(info)
        or not stat.S_ISREG(info.st_mode)
        or links != 1
    ):
        raise InstallError("install metadata must be a regular, singly linked file")
    try:
        encoded = path.read_bytes()
        if len(encoded) > MAX_CONFIG_BYTES:
            raise InstallError("install metadata is too large")
        raw = json.loads(encoded.decode("utf-8"), object_pairs_hook=_json_object)
    except InstallError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise InstallError(f"cannot parse install metadata: {exc}") from exc
    if not isinstance(raw, dict):
        raise InstallError("install metadata must be an object")
    required = {
        "formatVersion", "kind", "name", "source", "ref", "digest", "version",
        "updatedAt", "installedAt"
    }
    if set(raw) != required or raw.get("formatVersion") != 1:
        raise InstallError("install metadata has an unsupported format")
    if raw.get("kind") != "agent-skill-install" or raw.get("name") != name:
        raise InstallError("install metadata identity does not match the requested skill")
    if not isinstance(raw.get("digest"), str) or not re.fullmatch(
        r"[0-9a-f]{64}", str(raw["digest"])
    ):
        raise InstallError("install metadata digest is invalid")
    return raw


def _destination(args: argparse.Namespace, agent_home: Path) -> Path:
    if args.dest:
        return args.dest
    return agent_home / "skills"


def _add_source_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source")
    parser.add_argument("--ref")
    parser.add_argument("--allow-mutable-ref", action="store_true", default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="skill-installer")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="list catalogued skills")
    _add_source_options(listing)
    effective = commands.add_parser(
        "effective-config", help="print resolved installer configuration"
    )
    _add_source_options(effective)
    effective.add_argument("--agent-home", type=Path)
    for command, help_text in (
        ("install", "install one catalogued skill"),
        ("update", "safely update one installed skill to the approved release"),
    ):
        action = commands.add_parser(command, help=help_text)
        action.add_argument("name")
        _add_source_options(action)
        action.add_argument("--dest", type=Path)
        action.add_argument("--agent-home", type=Path)
        action.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
        action.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
        action.add_argument(
            "--stale-lock-seconds", type=int, default=DEFAULT_STALE_LOCK_SECONDS
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = resolve_effective_config(args)
        if args.command == "effective-config":
            output = {
                "source": _redact_source(config.source),
                "ref": config.ref,
                "allowMutableRef": config.allow_mutable_ref,
                "agentHome": str(config.agent_home),
                "origins": config.origins,
            }
            print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        release: ApprovedRelease | None = None
        content_ref = config.ref
        use_pointer = content_ref is None and (
            not _is_local_source(config.source)
            or (
                config.origins.get("source") != "current-checkout"
                and _local_has_approved_pointer(config.source)
            )
        )
        if use_pointer:
            release = fetch_approved_release(config.source)
            content_ref = release.ref
        with acquire_source(
            config.source, content_ref, config.allow_mutable_ref
        ) as (root, commit):
            catalog = load_catalog(root)
            if release is not None:
                validate_release_catalog(release, catalog, root / "catalog.json")
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
                item = catalog[args.name]
                destination = _destination(args, config.agent_home)
                approved_ref = commit or content_ref
                provenance = {
                    "source": _redact_source(
                        release.source if release is not None else config.source
                    ),
                    "ref": approved_ref,
                    "version": release.version if release else (
                        f"pinned-{approved_ref[:12]}" if approved_ref else "local-checkout"
                    ),
                    "updatedAt": release.updated_at if release else None,
                }
                replace = args.command == "update"
                if replace:
                    target = destination.expanduser().resolve() / args.name
                    if not target.exists() and not target.is_symlink():
                        raise InstallError(
                            f"skill is not installed; use install first: {target}"
                        )
                    metadata = load_install_metadata(destination, args.name)
                    installed_matches = False
                    try:
                        installed_files = inspect_tree(
                            target, args.max_files, args.max_bytes
                        )
                        validate_skill(target, args.name)
                        installed_matches = tree_digest(installed_files) == item["sha256"]
                    except InstallError:
                        installed_matches = False
                    if (
                        installed_matches
                        and metadata is not None
                        and metadata.get("source") == provenance["source"]
                        and metadata.get("ref") == provenance["ref"]
                        and metadata.get("digest") == item["sha256"]
                    ):
                        print(f"up-to-date\t{target}")
                        return 0
                installed = install_skill(
                    root,
                    item,
                    destination,
                    args.max_files,
                    args.max_bytes,
                    args.stale_lock_seconds,
                    replace=replace,
                    provenance=provenance,
                )
                print(installed)
        return 0
    except (InstallError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
