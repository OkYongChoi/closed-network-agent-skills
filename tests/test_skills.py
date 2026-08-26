from __future__ import annotations

import importlib.util
import io
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
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CREATOR_PATH = ROOT / "skills/skills-creator/scripts/skill_tool.py"
INSTALLER_PATH = ROOT / "skills/skills-installer/scripts/skill_installer.py"
SUMMARY_PATH = ROOT / "skills/repo-summary/scripts/repo_summary.py"
REFRESH_PATH = ROOT / "scripts/refresh_catalog.py"
VERIFY_PATH = ROOT / "scripts/verify_platform.py"
PROMOTE_PATH = ROOT / "scripts/promote_release.py"


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
promote = load_module("promote_release_test", PROMOTE_PATH)


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
    @staticmethod
    def config_args(**overrides):
        values = {
            "source": None,
            "ref": None,
            "allow_mutable_ref": None,
            "agent_home": None,
        }
        values.update(overrides)
        return types.SimpleNamespace(**values)

    def test_config_paths_are_platform_specific_and_injectable(self):
        user, system = installer.config_paths(
            environ={}, os_name="posix", home=Path("/home/tester")
        )
        self.assertEqual(user, Path("/home/tester/.agents/config.json"))
        self.assertEqual(system, Path("/etc/agent-tools/config.json"))
        user, system = installer.config_paths(
            environ={"USERPROFILE": "D:/Users/tester", "ProgramData": "D:/ProgramData"},
            os_name="nt",
            home=Path("unused"),
        )
        self.assertEqual(user, Path("D:/Users/tester/.agents/config.json"))
        self.assertEqual(system, Path("D:/ProgramData/AgentTools/config.json"))

    def test_effective_config_precedence_is_per_field(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            user_path = base / "user.json"
            system_path = base / "system.json"
            user_path.write_text(
                json.dumps(
                    {
                        "skills": {"source": "https://user/skills.git"},
                        "agentHome": "~/managed-agents",
                    }
                ),
                encoding="utf-8",
            )
            system_path.write_text(
                json.dumps(
                    {
                        "skills": {
                            "source": "https://system/skills.git",
                            "ref": "1" * 40,
                            "allowMutableRef": False,
                        },
                        "agentHome": "/system/agents",
                    }
                ),
                encoding="utf-8",
            )
            config = installer.resolve_effective_config(
                self.config_args(source="https://cli/skills.git"),
                environ={"AGENT_SKILLS_REF": "2" * 40},
                home=base / "profile",
                checkout=None,
                user_config_path=user_path,
                system_config_path=system_path,
            )
            self.assertEqual(config.source, "https://cli/skills.git")
            self.assertEqual(config.ref, "2" * 40)
            self.assertFalse(config.allow_mutable_ref)
            self.assertEqual(config.agent_home, base / "profile" / "managed-agents")
            self.assertEqual(config.origins["source"], "cli")
            self.assertEqual(config.origins["ref"], "env:AGENT_SKILLS_REF")
            self.assertTrue(config.origins["allowMutableRef"].startswith("system-config:"))
            self.assertTrue(config.origins["agentHome"].startswith("user-config:"))

    def test_environment_source_beats_user_config(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            user_path = base / "user.json"
            system_path = base / "missing-system.json"
            user_path.write_text(
                json.dumps({"skills": {"source": "https://user/repo.git", "ref": "1" * 40}}),
                encoding="utf-8",
            )
            config = installer.resolve_effective_config(
                self.config_args(),
                environ={"AGENT_SKILLS_SOURCE": "https://env/repo.git"},
                checkout=None,
                user_config_path=user_path,
                system_config_path=system_path,
            )
            self.assertEqual(config.source, "https://env/repo.git")
            self.assertEqual(config.ref, "1" * 40)

    def test_current_checkout_and_embedded_fallback_need_no_user_ref(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            missing_user = base / "user.json"
            missing_system = base / "system.json"
            local = installer.resolve_effective_config(
                self.config_args(),
                environ={},
                checkout=ROOT,
                user_config_path=missing_user,
                system_config_path=missing_system,
            )
            self.assertEqual(local.source, str(ROOT))
            self.assertIsNone(local.ref)
            with mock.patch.object(installer, "CANONICAL_REF", "a" * 40):
                fallback = installer.resolve_effective_config(
                    self.config_args(),
                    environ={},
                    checkout=None,
                    user_config_path=missing_user,
                    system_config_path=missing_system,
                )
            self.assertEqual(fallback.source, installer.CANONICAL_SOURCE)
            self.assertEqual(fallback.ref, "a" * 40)

    def test_explicit_canonical_source_without_ref_follows_approved_pointer(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            config = installer.resolve_effective_config(
                self.config_args(source=installer.CANONICAL_SOURCE),
                environ={},
                checkout=None,
                user_config_path=base / "missing-user.json",
                system_config_path=base / "missing-system.json",
            )
            self.assertEqual(config.source, installer.CANONICAL_SOURCE)
            self.assertIsNone(config.ref)
            self.assertEqual(config.origins["source"], "cli")
            self.assertEqual(config.origins["ref"], "unset")

    def test_mutable_ref_requires_explicit_effective_opt_in(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            user_path = base / "user.json"
            system_path = base / "system.json"
            user_path.write_text(
                json.dumps({"skills": {"source": "https://git/repo.git", "ref": "main"}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(installer.InstallError, "full 40-character"):
                installer.resolve_effective_config(
                    self.config_args(),
                    environ={},
                    checkout=None,
                    user_config_path=user_path,
                    system_config_path=system_path,
                )
            user_path.write_text(
                json.dumps(
                    {
                        "skills": {
                            "source": "https://git/repo.git",
                            "ref": "main",
                            "allowMutableRef": True,
                        }
                    }
                ),
                encoding="utf-8",
            )
            config = installer.resolve_effective_config(
                self.config_args(),
                environ={},
                checkout=None,
                user_config_path=user_path,
                system_config_path=system_path,
            )
            self.assertTrue(config.allow_mutable_ref)
            self.assertEqual(config.ref, "main")

    def test_mutable_ref_preserves_case_sensitive_branch_name(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            user_path = base / "user.json"
            system_path = base / "system.json"
            user_path.write_text(
                json.dumps(
                    {
                        "skills": {
                            "source": "https://git/repo.git",
                            "ref": "Release/Candidate-A",
                            "allowMutableRef": True,
                        }
                    }
                ),
                encoding="utf-8",
            )
            config = installer.resolve_effective_config(
                self.config_args(),
                environ={},
                checkout=None,
                user_config_path=user_path,
                system_config_path=system_path,
            )
            self.assertEqual(config.ref, "Release/Candidate-A")

    def test_present_invalid_config_fails_even_when_cli_overrides(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            user_path = base / "user.json"
            system_path = base / "system.json"
            for text, expected in (
                ('{"skills": {}, "skills": {}}', "duplicate key"),
                ('{"unknown": true}', "unknown top-level"),
                ('{"skills": {"allowMutableRef": "false"}}', "must be a boolean"),
            ):
                with self.subTest(text=text):
                    user_path.write_text(text, encoding="utf-8")
                    with self.assertRaisesRegex(installer.InstallError, expected):
                        installer.resolve_effective_config(
                            self.config_args(source=str(ROOT)),
                            environ={},
                            checkout=ROOT,
                            user_config_path=user_path,
                            system_config_path=system_path,
                        )

    def test_config_rejects_link_and_accepts_shared_plugins_section(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            real = base / "real.json"
            real.write_text(
                json.dumps(
                    {
                        "skills": {"source": str(ROOT)},
                        "plugins": {},
                    }
                ),
                encoding="utf-8",
            )
            loaded = installer.load_config(real)
            self.assertIsNotNone(loaded)
            link = base / "link.json"
            try:
                link.symlink_to(real)
            except (OSError, NotImplementedError):
                self.skipTest("symbolic links unavailable")
            with self.assertRaisesRegex(installer.InstallError, "regular, singly linked"):
                installer.load_config(link)

    def test_effective_config_redacts_url_credentials(self):
        self.assertEqual(
            installer._redact_source(
                "https://user:token@git.example:8443/skills.git?access_token=secret#x"
            ),
            "https://***@git.example:8443/skills.git",
        )

    def test_install_command_uses_central_config_without_source_or_ref_flags(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            skill = write_skill(source)
            write_catalog(source, skill)
            user_path = base / "profile" / ".agents" / "config.json"
            user_path.parent.mkdir(parents=True)
            user_path.write_text(
                json.dumps(
                    {
                        "skills": {"source": str(source)},
                        "agentHome": str(base / "managed-home"),
                    }
                ),
                encoding="utf-8",
            )
            system_path = base / "system.json"
            with mock.patch.object(
                installer, "config_paths", return_value=(user_path, system_path)
            ), redirect_stdout(io.StringIO()):
                self.assertEqual(installer.main(["install", "sample"]), 0)
            self.assertTrue(
                (base / "managed-home" / "skills" / "sample" / "SKILL.md").is_file()
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

    def test_windows_pid_probe_checks_exit_code_and_closes_handle(self):
        import ctypes

        open_process = mock.Mock(return_value=123)
        close_handle = mock.Mock(return_value=True)

        def exited(_handle, exit_code):
            exit_code._obj.value = 0
            return True

        get_exit_code = mock.Mock(side_effect=exited)
        kernel32 = types.SimpleNamespace(
            OpenProcess=open_process,
            CloseHandle=close_handle,
            GetExitCodeProcess=get_exit_code,
        )
        with mock.patch.object(
            ctypes, "WinDLL", return_value=kernel32, create=True
        ):
            self.assertFalse(installer._windows_pid_is_running(123456))
        get_exit_code.assert_called_once()
        close_handle.assert_called_once_with(123)

        def still_active(_handle, exit_code):
            exit_code._obj.value = 259
            return True

        get_exit_code.reset_mock(side_effect=True)
        get_exit_code.side_effect = still_active
        close_handle.reset_mock()
        with mock.patch.object(
            ctypes, "WinDLL", return_value=kernel32, create=True
        ):
            self.assertTrue(installer._windows_pid_is_running(123456))
        close_handle.assert_called_once_with(123)

        get_exit_code.reset_mock(side_effect=True)
        get_exit_code.return_value = False
        close_handle.reset_mock()
        with mock.patch.object(
            ctypes, "WinDLL", return_value=kernel32, create=True
        ):
            self.assertTrue(installer._windows_pid_is_running(123456))
        close_handle.assert_called_once_with(123)

        open_process.return_value = None
        close_handle.reset_mock()
        with mock.patch.object(
            ctypes, "WinDLL", return_value=kernel32, create=True
        ), mock.patch.object(ctypes, "get_last_error", return_value=5, create=True):
            self.assertTrue(installer._windows_pid_is_running(123456))
        close_handle.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows process-handle semantics")
    def test_windows_waited_subprocess_is_not_alive(self):
        process = subprocess.Popen([sys.executable, "-B", "-c", "pass"])
        process.wait(timeout=10)
        self.assertFalse(installer._pid_is_running(process.pid, platform="nt"))

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

    def test_release_manifest_is_strict_and_bound_to_source_and_catalog(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill = write_skill(root)
            write_catalog(root, skill)
            catalog_bytes = (root / "catalog.json").read_bytes()
            encoded = promote.manifest(
                catalog_bytes,
                "https://gitlab.example/ai/skills.git",
                "a" * 40,
                "2026.08.25.1",
                "2026-08-25T01:02:03Z",
            )
            release = installer._parse_release_manifest(
                encoded, expected_source="https://gitlab.example/ai/skills.git"
            )
            catalog = installer.load_catalog(root)
            installer.validate_release_catalog(release, catalog, root / "catalog.json")
            self.assertEqual(release.ref, "a" * 40)
            self.assertEqual(release.digests["sample"], catalog["sample"]["sha256"])
            with self.assertRaisesRegex(installer.InstallError, "configured source"):
                installer._parse_release_manifest(
                    encoded, expected_source="https://evil.example/skills.git"
                )
            tampered = json.loads(encoded)
            tampered["packages"][0]["digest"] = "0" * 64
            bad = installer._parse_release_manifest(
                json.dumps(tampered).encode(),
                expected_source="https://gitlab.example/ai/skills.git",
            )
            with self.assertRaisesRegex(installer.InstallError, "packages"):
                installer.validate_release_catalog(bad, catalog, root / "catalog.json")

    def test_latest_approved_pointer_resolves_to_immutable_content_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            repository = Path(temp) / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
            skill = write_skill(repository)
            write_catalog(repository, skill)
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(["git", "-c", "commit.gpgSign=false", "commit", "--quiet", "-m", "content"], cwd=repository, check=True)
            content_ref = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
            catalog_bytes = (repository / "catalog.json").read_bytes()
            source = repository.as_uri()
            encoded = promote.manifest(
                catalog_bytes, source, content_ref, "approved-1", "2026-08-25T01:02:03Z"
            )
            subprocess.run(["git", "checkout", "--quiet", "--orphan", "latest-approved"], cwd=repository, check=True)
            subprocess.run(["git", "rm", "-rf", "--quiet", "."], cwd=repository, check=True)
            (repository / "release-manifest.json").write_bytes(encoded)
            subprocess.run(["git", "add", "release-manifest.json"], cwd=repository, check=True)
            subprocess.run(["git", "-c", "commit.gpgSign=false", "commit", "--quiet", "-m", "approve"], cwd=repository, check=True)
            release = installer.fetch_approved_release(source)
            self.assertEqual(release.ref, content_ref)
            with installer.acquire_source(source, release.ref, False) as (root, resolved):
                self.assertEqual(resolved, content_ref)
                catalog = installer.load_catalog(root)
                installer.validate_release_catalog(release, catalog, root / "catalog.json")
            mirror = Path(temp) / "skills.git"
            subprocess.run(
                ["git", "clone", "--quiet", "--mirror", str(repository), str(mirror)],
                check=True,
            )
            local_release = installer.fetch_approved_release(str(mirror))
            self.assertTrue(installer._local_has_approved_pointer(str(mirror)))
            self.assertTrue(installer._local_has_approved_pointer(str(repository)))
            self.assertFalse(installer._local_has_approved_pointer(str(ROOT)))
            self.assertEqual(local_release.source, source)
            self.assertEqual(local_release.ref, content_ref)
            with installer.acquire_source(
                str(mirror), local_release.ref, False
            ) as (root, resolved):
                self.assertEqual(resolved, content_ref)
                installer.validate_release_catalog(
                    local_release, installer.load_catalog(root), root / "catalog.json"
                )

    def test_update_replaces_atomically_and_preserves_sidecar_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            destination = base / "installed"
            skill = write_skill(source, extra={"version.txt": b"one"})
            write_catalog(source, skill)
            item = installer.load_catalog(source)["sample"]
            first = {"source": "mirror", "ref": "1" * 40, "version": "v1", "updatedAt": None}
            target = installer.install_skill(
                source, item, destination, 100, 1_000_000, provenance=first
            )
            self.assertEqual((target / "version.txt").read_bytes(), b"one")
            self.assertFalse((target / installer.INSTALL_METADATA_DIR).exists())
            (skill / "version.txt").write_bytes(b"two")
            write_catalog(source, skill)
            item = installer.load_catalog(source)["sample"]
            second = {"source": "mirror", "ref": "2" * 40, "version": "v2", "updatedAt": None}
            installer.install_skill(
                source, item, destination, 100, 1_000_000,
                replace=True, provenance=second
            )
            self.assertEqual((target / "version.txt").read_bytes(), b"two")
            metadata = installer.load_install_metadata(destination, "sample")
            self.assertEqual(metadata["ref"], "2" * 40)
            self.assertEqual(metadata["digest"], item["sha256"])

    def test_update_rolls_back_when_metadata_commit_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            destination = base / "installed"
            skill = write_skill(source, extra={"version.txt": b"stable"})
            write_catalog(source, skill)
            old_item = installer.load_catalog(source)["sample"]
            provenance = {"source": "mirror", "ref": "1" * 40, "version": "v1", "updatedAt": None}
            target = installer.install_skill(
                source, old_item, destination, 100, 1_000_000, provenance=provenance
            )
            old_metadata = installer.load_install_metadata(destination, "sample")
            (skill / "version.txt").write_bytes(b"broken")
            write_catalog(source, skill)
            new_item = installer.load_catalog(source)["sample"]
            real_replace = installer.os.replace

            def fail_metadata(source_path, destination_path):
                if str(source_path).endswith(".tmp"):
                    raise OSError("simulated metadata failure")
                return real_replace(source_path, destination_path)

            with mock.patch.object(installer.os, "replace", side_effect=fail_metadata):
                with self.assertRaisesRegex(OSError, "simulated"):
                    installer.install_skill(
                        source, new_item, destination, 100, 1_000_000,
                        replace=True,
                        provenance={"source": "mirror", "ref": "2" * 40, "version": "v2", "updatedAt": None},
                    )
            self.assertEqual((target / "version.txt").read_bytes(), b"stable")
            self.assertEqual(installer.load_install_metadata(destination, "sample"), old_metadata)

    def test_update_rolls_back_on_keyboard_interrupt(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            destination = base / "installed"
            skill = write_skill(source, extra={"version.txt": b"stable"})
            write_catalog(source, skill)
            old_item = installer.load_catalog(source)["sample"]
            first = {"source": "mirror", "ref": "1" * 40, "version": "v1", "updatedAt": None}
            target = installer.install_skill(
                source, old_item, destination, 100, 1_000_000, provenance=first
            )
            old_metadata = installer.load_install_metadata(destination, "sample")
            (skill / "version.txt").write_bytes(b"interrupted")
            write_catalog(source, skill)
            new_item = installer.load_catalog(source)["sample"]
            real_replace = installer.os.replace

            def interrupt_metadata(source_path, destination_path):
                if str(source_path).endswith(".tmp"):
                    real_replace(source_path, destination_path)
                    raise KeyboardInterrupt()
                return real_replace(source_path, destination_path)

            with mock.patch.object(installer.os, "replace", side_effect=interrupt_metadata):
                with self.assertRaises(KeyboardInterrupt):
                    installer.install_skill(
                        source, new_item, destination, 100, 1_000_000,
                        replace=True,
                        provenance={"source": "mirror", "ref": "2" * 40, "version": "v2", "updatedAt": None},
                    )
            self.assertEqual((target / "version.txt").read_bytes(), b"stable")
            self.assertEqual(installer.load_install_metadata(destination, "sample"), old_metadata)

    def test_release_publisher_skips_stale_sequence_and_allows_audited_rollback(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            content = base / "content"
            content.mkdir()
            subprocess.run(["git", "init", "--quiet"], cwd=content, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=content, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=content, check=True)
            skill = write_skill(content)
            write_catalog(content, skill)
            subprocess.run(["git", "add", "."], cwd=content, check=True)
            subprocess.run(["git", "-c", "commit.gpgSign=false", "commit", "--quiet", "-m", "v1"], cwd=content, check=True)
            v1 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=content, text=True).strip()
            (skill / "unapproved.txt").write_text("not released", encoding="utf-8")
            write_catalog(content, skill)
            subprocess.run(["git", "add", "."], cwd=content, check=True)
            subprocess.run(["git", "-c", "commit.gpgSign=false", "commit", "--quiet", "-m", "unapproved"], cwd=content, check=True)
            unapproved = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=content, text=True).strip()
            (skill / "new.txt").write_text("v2", encoding="utf-8")
            write_catalog(content, skill)
            subprocess.run(["git", "add", "."], cwd=content, check=True)
            subprocess.run(["git", "-c", "commit.gpgSign=false", "commit", "--quiet", "-m", "v2"], cwd=content, check=True)
            v2 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=content, text=True).strip()
            mirror = base / "skills.git"
            subprocess.run(["git", "clone", "--quiet", "--mirror", str(content), str(mirror)], check=True)
            common = {
                "source": "https://gitlab.example/ai/skills.git",
                "push_url_env": "TEST_PUSH_URL",
                "updated_at": "2026-08-25T01:02:03Z",
                "git_name": "Test",
                "git_email": "test@example.invalid",
            }
            with mock.patch.dict(os.environ, {"TEST_PUSH_URL": str(mirror)}):
                promote.publish(types.SimpleNamespace(
                    **{**common, "source": "https://github.example/ai/skills.git"},
                    ref=v1, version="approved-10", sequence=10, rollback=False,
                ))
                promote.publish(types.SimpleNamespace(
                    **common, ref=v2, version="approved-20", sequence=20, rollback=False
                ))
                first_pointer = subprocess.check_output(
                    ["git", "--git-dir", str(mirror), "rev-parse", "latest-approved"], text=True
                ).strip()
                promote.publish(types.SimpleNamespace(
                    **common, ref=v1, version="approved-19", sequence=19, rollback=False
                ))
                self.assertEqual(
                    subprocess.check_output(
                        ["git", "--git-dir", str(mirror), "rev-parse", "latest-approved"], text=True
                    ).strip(),
                    first_pointer,
                )
                with self.assertRaisesRegex(promote.PromoteError, "approval history"):
                    promote.publish(types.SimpleNamespace(
                        **common, ref=unapproved, version="rollback-21", sequence=21,
                        rollback=True,
                    ))
                promote.publish(types.SimpleNamespace(
                    **common, ref=v1, version="rollback-5", sequence=5, rollback=True
                ))
                promote.publish(types.SimpleNamespace(
                    **common, ref=v2, version="approved-20-retry", sequence=20,
                    rollback=False,
                ))
            release = installer.fetch_approved_release(str(mirror))
            self.assertEqual(release.ref, v1)
            self.assertEqual(release.version, "rollback-5")
            self.assertEqual(release.sequence, 21)


class RepositoryTests(unittest.TestCase):
    def test_gitlab_auto_publish_is_limited_to_default_branch_pushes(self):
        pipeline = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")
        self.assertIn(
            '$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH && $CI_PIPELINE_SOURCE == "push"',
            pipeline,
        )

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
