from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CREATOR_PATH = ROOT / "skills/skills-creator/scripts/skill_tool.py"
INSTALLER_PATH = ROOT / "skills/skills-installer/scripts/skill_installer.py"
SUMMARY_PATH = ROOT / "skills/repo-summary/scripts/repo_summary.py"
REFRESH_PATH = ROOT / "scripts/refresh_catalog.py"
VERIFY_PATH = ROOT / "scripts/verify_platform.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


creator = load_module("skill_tool_test", CREATOR_PATH)
installer = load_module("skill_installer_test", INSTALLER_PATH)
summary = load_module("repo_summary_test", SUMMARY_PATH)
refresh = load_module("refresh_catalog_test", REFRESH_PATH)
platform_verify = load_module("verify_platform_test", VERIFY_PATH)


@contextmanager
def environment(**values: str | None):
    original = os.environ.copy()
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def write_skill(root: Path, name: str = "sample", extra: dict[str, bytes] | None = None) -> Path:
    skill = root / "skills" / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: \"{name}\"\ndescription: \"Test helper. Use in installer tests.\"\n---\n\n# Test\n",
        encoding="utf-8",
    )
    for relative, content in (extra or {}).items():
        path = skill / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return skill


def write_catalog(root: Path, skill: Path, digest: str | None = None) -> None:
    files = installer.inspect_tree(skill)
    data = {
        "format_version": 1,
        "digest_algorithm": installer.DIGEST_ALGORITHM,
        "skills": [
            {
                "name": skill.name,
                "description": "test",
                "path": f"skills/{skill.name}",
                "sha256": digest or installer.tree_digest(files),
            }
        ],
    }
    (root / "catalog.json").write_text(json.dumps(data), encoding="utf-8")


class CreatorTests(unittest.TestCase):
    def test_create_and_validate(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            target = creator.create_skill(
                "incident-brief",
                output,
                "Create incident briefs. Use during engineering handoff.",
                ["scripts", "references"],
            )
            fields = creator.validate_skill(target)
            self.assertEqual(fields["name"], "incident-brief")
            self.assertIn('name: "incident-brief"', (target / "SKILL.md").read_text())
            self.assertTrue((target / "scripts").is_dir())

    def test_rejects_invalid_name_and_unknown_yaml(self):
        for name in ("Bad_Name", "con", "aux.txt"):
            with self.subTest(name=name), self.assertRaises(creator.SkillError):
                creator.validate_name(name)
        with tempfile.TemporaryDirectory() as temp:
            skill = Path(temp) / "sample"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "---\nname: sample\ndescription: ok\nmetadata:\n---\nbody\n",
                encoding="utf-8",
            )
            with self.assertRaises(creator.SkillError):
                creator.validate_skill(skill)

    def test_rejects_ambiguous_plain_scalars(self):
        for value in ("yes", "null", "123", "two words", "value:other"):
            with self.subTest(value=value), self.assertRaises(creator.SkillError):
                creator._parse_scalar(value, 2)
        self.assertEqual(creator._parse_scalar("Apache-2.0", 2), "Apache-2.0")

    def test_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            (output / "sample").mkdir()
            with self.assertRaises(creator.SkillError):
                creator.create_skill("sample", output, "Use for a test.", [])

    def test_rejects_nonportable_tree_paths(self):
        for relative in (
            "CON.txt",
            "bad:name.txt",
            "trailing.",
            "e\N{COMBINING ACUTE ACCENT}.txt",
            "x" * 256,
        ):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temp:
                with self.assertRaises(creator.portable_paths.PortablePathError):
                    creator.portable_paths.validate_portable_relative_path(relative)
                skill = write_skill(Path(temp))
                candidate = skill / relative
                try:
                    candidate.write_text("x", encoding="utf-8")
                except OSError:
                    continue
                # NTFS may treat ':' as an alternate data stream and Win32 may
                # trim trailing dots/spaces. Exercise the tree scanner only when
                # the directory entry round-trips the hostile spelling exactly.
                with os.scandir(skill) as entries:
                    materialized_names = {entry.name for entry in entries}
                if relative not in materialized_names:
                    continue
                with self.assertRaises(creator.SkillError):
                    creator.validate_skill(skill)

    def test_creator_rejects_hard_linked_files(self):
        with tempfile.TemporaryDirectory() as temp:
            skill = write_skill(Path(temp), extra={"data.txt": b"data"})
            try:
                os.link(skill / "data.txt", skill / "duplicate.txt")
            except (OSError, NotImplementedError):
                self.skipTest("hard links are unavailable for this runner")
            with self.assertRaisesRegex(creator.SkillError, "hard-linked"):
                creator.validate_skill(skill)


class InstallerTests(unittest.TestCase):
    def test_source_precedence(self):
        with environment(AGENT_SKILLS_SOURCE="/environment/source"):
            self.assertEqual(installer.select_source("/cli/source"), "/cli/source")
            self.assertEqual(installer.select_source(None), "/environment/source")
        with environment(AGENT_SKILLS_SOURCE=None):
            self.assertEqual(Path(installer.select_source(None)), ROOT)

    def test_ref_precedence_and_catalog_canonical_ref(self):
        catalog_ref = "1" * 40
        with tempfile.TemporaryDirectory() as temp:
            checkout = Path(temp)
            (checkout / "catalog.json").write_text(
                json.dumps({"repository": {"ref": catalog_ref}}), encoding="utf-8"
            )
            with mock.patch.object(installer, "_checkout_root", return_value=checkout):
                with environment(AGENT_SKILLS_REF=None):
                    self.assertEqual(
                        installer.select_ref(None, installer.CANONICAL_SOURCE), catalog_ref
                    )
                with environment(AGENT_SKILLS_REF="2" * 40):
                    self.assertEqual(
                        installer.select_ref(None, installer.CANONICAL_SOURCE), "2" * 40
                    )
                self.assertEqual(
                    installer.select_ref("3" * 40, installer.CANONICAL_SOURCE), "3" * 40
                )
            with mock.patch.object(installer, "_checkout_root", return_value=None), mock.patch.object(
                installer, "CANONICAL_REF", "4" * 40
            ), environment(AGENT_SKILLS_REF=None):
                self.assertEqual(
                    installer.select_ref(None, installer.CANONICAL_SOURCE), "4" * 40
                )

    def test_remote_requires_full_sha(self):
        with self.assertRaises(installer.InstallError):
            with installer.acquire_source("https://example.invalid/skills.git", None, False):
                pass
        with self.assertRaises(installer.InstallError):
            with installer.acquire_source("https://example.invalid/skills.git", "main", False):
                pass

    def test_install_and_reject_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            destination = base / "destination"
            skill = write_skill(source)
            write_catalog(source, skill)
            item = installer.load_catalog(source)["sample"]
            target = installer.install_skill(source, item, destination, 100, 1_000_000)
            self.assertEqual((target / "SKILL.md").read_text(), (skill / "SKILL.md").read_text())
            with self.assertRaises(installer.InstallError):
                installer.install_skill(source, item, destination, 100, 1_000_000)

    def test_checksum_failure_leaves_no_partial_target(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            destination = base / "destination"
            skill = write_skill(source)
            write_catalog(source, skill, "0" * 64)
            item = installer.load_catalog(source)["sample"]
            with self.assertRaises(installer.InstallError):
                installer.install_skill(source, item, destination, 100, 1_000_000)
            self.assertFalse((destination / "sample").exists())

    def test_rejects_symlink_hardlink_and_size_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            skill = write_skill(base, extra={"data.txt": b"12345"})
            try:
                (skill / "link").symlink_to("data.txt")
            except (OSError, NotImplementedError):
                self.skipTest("symbolic links are unavailable for this runner")
            with self.assertRaisesRegex(installer.InstallError, "symbolic"):
                installer.inspect_tree(skill)
            (skill / "link").unlink()
            try:
                os.link(skill / "data.txt", skill / "hardlink")
            except (OSError, NotImplementedError):
                self.skipTest("hard links are unavailable for this runner")
            with self.assertRaisesRegex(installer.InstallError, "hard-linked"):
                installer.inspect_tree(skill)
            (skill / "hardlink").unlink()
            with self.assertRaisesRegex(installer.InstallError, "byte limit"):
                installer.inspect_tree(skill, max_bytes=4)

    def test_rejects_case_collision_where_supported(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            skill = write_skill(base)
            (skill / "Alpha").mkdir()
            (skill / "Alpha/a.txt").write_text("a")
            try:
                (skill / "alpha").mkdir()
            except FileExistsError:
                self.skipTest("filesystem is case-insensitive")
            (skill / "alpha/b.txt").write_text("b")
            if len([p for p in skill.iterdir() if p.name.casefold() == "alpha"]) < 2:
                self.skipTest("filesystem is case-insensitive")
            with self.assertRaisesRegex(installer.InstallError, "case-colliding"):
                installer.inspect_tree(skill)

    def test_runtime_artifacts_are_ignored_before_link_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            skill = write_skill(base)
            outside = base / "outside"
            outside.mkdir()
            try:
                (skill / "__pycache__").symlink_to(outside, target_is_directory=True)
                (skill / ".DS_Store").symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("symbolic links are unavailable for this runner")
            paths = [item[1] for item in installer.inspect_tree(skill)]
            self.assertEqual(paths, ["SKILL.md"])
            self.assertEqual(refresh.digest_tree(skill), installer.tree_digest(installer.inspect_tree(skill)))

    def test_digest_is_independent_of_posix_file_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            skill = write_skill(Path(temp), extra={"scripts/run.py": b"print('ok')\n"})
            script = skill / "scripts/run.py"
            script.chmod(0o644)
            first = installer.tree_digest(installer.inspect_tree(skill))
            script.chmod(0o755)
            second = installer.tree_digest(installer.inspect_tree(skill))
            self.assertEqual(first, second)
            write_catalog(Path(temp), skill)
            item = installer.load_catalog(Path(temp))["sample"]
            target = installer.install_skill(Path(temp), item, Path(temp) / "installed", 100, 1_000_000)
            if os.name != "nt":
                self.assertEqual((target / "scripts/run.py").stat().st_mode & 0o777, 0o755)

    @unittest.skipIf(os.name == "nt", "POSIX executable bit semantics")
    def test_posix_install_preserves_executable_source_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill = write_skill(root, extra={"scripts/run.py": b"print('ok')\n"})
            (skill / "scripts/run.py").chmod(0o755)
            write_catalog(root, skill)
            item = installer.load_catalog(root)["sample"]
            target = installer.install_skill(root, item, root / "installed", 100, 1_000_000)
            self.assertEqual((target / "scripts/run.py").stat().st_mode & 0o777, 0o755)

    def test_rejects_windows_reserved_and_ambiguous_paths(self):
        invalid = (
            "skills/CON.txt",
            "skills/COM¹.txt",
            "skills/CON .txt",
            "skills/bad:name",
            "skills/trailing. ",
            "skills\\sample",
            "C:/skills/sample",
            "skills/e\N{COMBINING ACUTE ACCENT}",
            "skills/" + "x" * 256,
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(installer.InstallError):
                installer._safe_catalog_path(value)

    def test_portable_key_detects_unicode_case_collision(self):
        first = installer.portable_paths.validate_portable_relative_path(
            "data/Stra\N{LATIN SMALL LETTER SHARP S}e.txt"
        )
        second = installer.portable_paths.validate_portable_relative_path(
            "data/STRASSE.txt"
        )
        self.assertEqual(
            installer.portable_paths.portable_path_key(first),
            installer.portable_paths.portable_path_key(second),
        )

    def test_windows_reparse_attribute_is_rejected(self):
        reparse = types.SimpleNamespace(
            st_file_attributes=installer.portable_paths.FILE_ATTRIBUTE_REPARSE_POINT
        )
        regular = types.SimpleNamespace(st_file_attributes=0)
        self.assertTrue(installer.portable_paths.is_windows_reparse_point(reparse))
        self.assertFalse(installer.portable_paths.is_windows_reparse_point(regular))

    def test_zero_direntry_link_count_is_refreshed(self):
        class ZeroLinkStat:
            def __init__(self, wrapped):
                self.wrapped = wrapped
                self.st_nlink = 0

            def __getattr__(self, name):
                return getattr(self.wrapped, name)

        class ZeroLinkEntry:
            def __init__(self, wrapped):
                self.wrapped = wrapped
                self.name = wrapped.name
                self.path = wrapped.path

            def stat(self, *, follow_symlinks=True):
                result = self.wrapped.stat(follow_symlinks=follow_symlinks)
                if stat.S_ISREG(result.st_mode):
                    return ZeroLinkStat(result)
                return result

            def is_symlink(self):
                return self.wrapped.is_symlink()

            def is_dir(self, *, follow_symlinks=True):
                return self.wrapped.is_dir(follow_symlinks=follow_symlinks)

            def is_file(self, *, follow_symlinks=True):
                return self.wrapped.is_file(follow_symlinks=follow_symlinks)

        actual_scandir = os.scandir

        def zero_link_scandir(path):
            with actual_scandir(path) as entries:
                return [ZeroLinkEntry(entry) for entry in entries]

        with tempfile.TemporaryDirectory() as temp:
            skill = write_skill(Path(temp), extra={"data.txt": b"data"})
            with mock.patch.object(installer.os, "scandir", side_effect=zero_link_scandir):
                files = installer.inspect_tree(skill)
                self.assertTrue(all(info.st_nlink == 1 for _, _, info in files))
                creator.validate_skill(skill)

    def test_windows_link_count_api_is_final_fallback(self):
        incomplete = types.SimpleNamespace(st_nlink=0)
        refreshed = types.SimpleNamespace(st_nlink=0)
        path = Path("unresolved-file")
        with mock.patch.object(
            installer.portable_paths.os, "stat", return_value=refreshed
        ), mock.patch.object(
            installer.portable_paths, "_windows_file_link_count", return_value=1
        ) as windows_count:
            self.assertEqual(
                installer.portable_paths.file_link_count(
                    path, incomplete, platform="nt"
                ),
                1,
            )
            windows_count.assert_called_once_with(path)

    @unittest.skipUnless(os.name == "nt", "Windows hard-link semantics")
    def test_windows_real_hard_link_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            skill = write_skill(Path(temp), extra={"data.txt": b"data"})
            try:
                os.link(skill / "data.txt", skill / "duplicate.txt")
            except OSError as exc:
                self.skipTest(f"hard-link creation is unavailable: {exc}")
            with self.assertRaisesRegex(installer.InstallError, "hard-linked"):
                installer.inspect_tree(skill)
            with self.assertRaisesRegex(creator.SkillError, "hard-linked"):
                creator.validate_skill(skill)

    def test_catalog_reparse_attribute_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "catalog.json").write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "digest_algorithm": installer.DIGEST_ALGORITHM,
                        "skills": [],
                    }
                ),
                encoding="utf-8",
            )
            original_lstat = Path.lstat

            def simulated_lstat(path: Path):
                result = original_lstat(path)
                if path.name == "catalog.json":
                    return types.SimpleNamespace(
                        st_mode=stat.S_IFREG | 0o644,
                        st_file_attributes=installer.portable_paths.FILE_ATTRIBUTE_REPARSE_POINT,
                    )
                return result

            with mock.patch.object(Path, "lstat", new=simulated_lstat), self.assertRaisesRegex(
                installer.InstallError, "regular file"
            ):
                installer.load_catalog(root)

    def test_rejects_catalog_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = {
                "format_version": 1,
                "digest_algorithm": installer.DIGEST_ALGORITHM,
                "skills": [{"name": "sample", "path": "../sample", "sha256": "0" * 64}],
            }
            (root / "catalog.json").write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(installer.InstallError, "unsafe catalog path"):
                installer.load_catalog(root)

    def test_rejects_portable_colliding_catalog_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = {
                "format_version": 1,
                "digest_algorithm": installer.DIGEST_ALGORITHM,
                "skills": [
                    {"name": "first", "path": "skills/Alpha", "sha256": "0" * 64},
                    {"name": "second", "path": "skills/alpha", "sha256": "1" * 64},
                ],
            }
            (root / "catalog.json").write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(installer.InstallError, "colliding catalog"):
                installer.load_catalog(root)

    def test_rejects_linked_catalog_and_skill_path(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            source.mkdir()
            real_catalog = base / "catalog.json"
            real_catalog.write_text('{"format_version": 1, "skills": []}', encoding="utf-8")
            try:
                (source / "catalog.json").symlink_to(real_catalog)
            except (OSError, NotImplementedError):
                self.skipTest("symbolic links are unavailable for this runner")
            with self.assertRaisesRegex(installer.InstallError, "regular file"):
                installer.load_catalog(source)
            (source / "catalog.json").unlink()
            real_skills = base / "real-skills"
            skill = write_skill(base / "fixture")
            real_skills.mkdir()
            shutil.move(str(skill), real_skills / "sample")
            (source / "skills").symlink_to(real_skills, target_is_directory=True)
            item = {"name": "sample", "path": "skills/sample", "sha256": "0" * 64}
            with self.assertRaisesRegex(installer.InstallError, "symbolic link"):
                installer._resolve_skill_path(source, item)

    def test_existing_lock_blocks_concurrent_install(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            destination = base / "destination"
            skill = write_skill(source)
            write_catalog(source, skill)
            destination.mkdir()
            (destination / ".sample.install.lock").write_text("busy")
            item = installer.load_catalog(source)["sample"]
            with self.assertRaisesRegex(installer.InstallError, "in progress"):
                installer.install_skill(source, item, destination, 100, 1_000_000)
            self.assertFalse((destination / "sample").exists())

    def test_recovers_old_same_host_dead_process_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            destination = base / "destination"
            skill = write_skill(source)
            write_catalog(source, skill)
            destination.mkdir()
            lock = destination / ".sample.install.lock"
            lock.write_text(
                json.dumps(
                    {"pid": 999999999, "host": socket.gethostname(), "created": time.time() - 60}
                ),
                encoding="utf-8",
            )
            old = time.time() - 60
            os.utime(lock, (old, old))
            item = installer.load_catalog(source)["sample"]
            target = installer.install_skill(
                source, item, destination, 100, 1_000_000, stale_lock_seconds=1
            )
            self.assertTrue(target.is_dir())
            self.assertFalse(lock.exists())

    def test_stale_lock_recovery_fails_closed_for_foreign_or_live_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lock = root / "lock"
            for metadata in (
                {"pid": 999999999, "host": "another-host.invalid", "created": time.time() - 60},
                {"pid": os.getpid(), "host": socket.gethostname(), "created": time.time() - 60},
            ):
                with self.subTest(metadata=metadata):
                    lock.write_text(json.dumps(metadata), encoding="utf-8")
                    old = time.time() - 60
                    os.utime(lock, (old, old))
                    self.assertFalse(installer._recover_stale_lock(lock, stale_after=1))
                    self.assertTrue(lock.exists())
                    lock.unlink()

    def test_windows_pid_probe_never_calls_os_kill(self):
        with mock.patch.object(
            installer, "_windows_pid_is_running", return_value=False
        ) as windows_probe, mock.patch.object(installer.os, "kill") as kill:
            self.assertFalse(installer._pid_is_running(123456, platform="nt"))
            windows_probe.assert_called_once_with(123456)
            kill.assert_not_called()

    def test_full_sha_remote_clone_and_install(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            repository = base / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
            skill = write_skill(repository)
            write_catalog(repository, skill)
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "commit.gpgSign=false",
                    "commit",
                    "--quiet",
                    "-m",
                    "fixture",
                ],
                cwd=repository,
                check=True,
            )
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
            with installer.acquire_source(repository.as_uri(), commit, False) as (root, resolved):
                self.assertEqual(resolved, commit)
                item = installer.load_catalog(root)["sample"]
                target = installer.install_skill(root, item, base / "installed", 100, 1_000_000)
                self.assertTrue((target / "SKILL.md").is_file())


class RepositoryTests(unittest.TestCase):
    def test_nul_delimited_git_attribute_parser(self):
        parsed = platform_verify.parse_check_attr_z(
            b"skills/sample/SKILL.md\0text\0set\0"
            b"skills/sample/SKILL.md\0eol\0lf\0"
        )
        self.assertEqual(
            parsed,
            {"skills/sample/SKILL.md": {"text": "set", "eol": "lf"}},
        )
        with self.assertRaises(platform_verify.VerifyError):
            platform_verify.parse_check_attr_z(b"path\0text\0")

    def test_catalog_is_current(self):
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "scripts/refresh_catalog.py"), "--check"],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_portable_path_modules_are_kept_in_sync(self):
        creator_paths = ROOT / "skills/skills-creator/scripts/portable_paths.py"
        installer_paths = ROOT / "skills/skills-installer/scripts/portable_paths.py"
        self.assertEqual(creator_paths.read_bytes(), installer_paths.read_bytes())

    def test_all_catalogued_skills_validate(self):
        catalog = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(
            {item["name"] for item in catalog["skills"]},
            {"skills-creator", "skills-installer", "repo-summary"},
        )
        for item in catalog["skills"]:
            creator.validate_skill(ROOT / item["path"])

    def test_repo_summary_is_bounded_and_offline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "package.json").write_text(
                json.dumps({"scripts": {"test": "node --test"}}), encoding="utf-8"
            )
            (root / "src").mkdir()
            (root / "src/app.ts").write_text("export {}", encoding="utf-8")
            data = summary.summarize(root, 100, 100_000)
            self.assertEqual(data["languages"], {"TypeScript": 1})
            self.assertIn("npm run test", data["commands"])
            rendered = summary.render_markdown(data)
            self.assertIn("- TypeScript: 1 files", rendered)
            self.assertNotIn("- None detected", rendered)
            with mock.patch.object(subprocess, "Popen", side_effect=FileNotFoundError):
                # Non-Git directories tolerate an unavailable Git executable and never use a network API.
                data = summary.summarize(root, 100, 100_000)
                self.assertIsNone(data["git"]["branch"])

    def test_repo_summary_bounds_manifest_and_git_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            oversized = b'{"scripts":{"test":"x"}}' + b" " * summary.MAX_MANIFEST_BYTES
            (root / "package.json").write_bytes(oversized)
            self.assertEqual(summary._package_commands(root / "package.json"), [])
            subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
            for index in range(20):
                (root / f"untracked-{index:02d}-with-a-long-name.txt").write_text("x")
            output, truncated = summary._git(root, "status", "--porcelain", max_output=64)
            self.assertTrue(truncated)
            self.assertLessEqual(len(output.encode("utf-8")), 64)

    def test_repo_summary_detects_windows_and_dotnet_projects(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in (
                "Demo.sln",
                "Demo.csproj",
                "Directory.Build.props",
                "Directory.Build.targets",
                "build.gradle",
                "gradlew",
                "gradlew.bat",
                "bootstrap.ps1",
                "build.cmd",
                "legacy.bat",
                "Program.cs",
            ):
                (root / name).write_text("", encoding="utf-8")
            data = summary.summarize(root, 100, 1024 * 1024)
            self.assertTrue(
                {
                    "Demo.sln",
                    "Demo.csproj",
                    "Directory.Build.props",
                    "Directory.Build.targets",
                    "gradlew.bat",
                }
                <= set(data["manifests"])
            )
            self.assertTrue(
                {
                    "dotnet build",
                    "dotnet test",
                    r".\gradlew.bat build",
                    r".\gradlew.bat test",
                    "./gradlew build",
                    "./gradlew test",
                    "pwsh -File ./bootstrap.ps1",
                    r".\build.cmd",
                    r".\legacy.bat",
                }
                <= set(data["commands"])
            )
            self.assertEqual(data["languages"]["PowerShell"], 1)
            self.assertEqual(data["languages"]["Windows Batch"], 3)
            self.assertEqual(data["languages"]["C#"], 1)

    def test_repo_summary_prunes_simulated_windows_reparse_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            linked = root / "linked"
            linked.mkdir()
            linked = linked.resolve()
            (linked / "outside.py").write_text("print('outside')", encoding="utf-8")
            original_lstat = Path.lstat

            def simulated_lstat(path: Path):
                result = original_lstat(path)
                if path == linked:
                    return types.SimpleNamespace(
                        st_file_attributes=summary.FILE_ATTRIBUTE_REPARSE_POINT
                    )
                return result

            with mock.patch.object(Path, "lstat", new=simulated_lstat):
                data = summary.summarize(root, 100, 1024 * 1024)
            self.assertNotIn("Python", data["languages"])
            self.assertIn("link/reparse directory: linked", data["risks"])

    @unittest.skipUnless(os.name == "nt", "junction semantics require Windows")
    def test_repo_summary_prunes_windows_junction(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "repository"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            (outside / "outside.py").write_text("print('outside')", encoding="utf-8")
            junction = root / "linked"
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            if result.returncode != 0:
                self.skipTest(f"junction creation is unavailable: {result.stdout.strip()}")
            data = summary.summarize(root, 100, 1024 * 1024)
            self.assertNotIn("Python", data["languages"])
            self.assertIn("link/reparse directory: linked", data["risks"])


if __name__ == "__main__":
    unittest.main()
