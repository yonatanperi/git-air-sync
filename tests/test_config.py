"""Config persistence: round-trip, atomicity, tolerance of odd input."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from git_air_sync import config as config_mod


class ConfigBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        self.path = self.root / "config.json"
        self._original = os.environ.get("AIR_SYNC_CONFIG")
        os.environ["AIR_SYNC_CONFIG"] = str(self.path)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._original is None:
            os.environ.pop("AIR_SYNC_CONFIG", None)
        else:
            os.environ["AIR_SYNC_CONFIG"] = self._original


class LoadSave(ConfigBase):
    def test_missing_file_gives_defaults(self) -> None:
        cfg = config_mod.load()
        self.assertIsNone(cfg.machine_role)
        self.assertEqual(cfg.projects, {})
        self.assertFalse(config_mod.exists())

    def test_round_trip(self) -> None:
        cfg = config_mod.load()
        cfg.machine_role = "A"
        cfg.projects_root = "/tmp/projects"
        cfg.default_project = "alpha"
        cfg.project("alpha").last_synced_commit = "a" * 40
        config_mod.save(cfg)

        again = config_mod.load()
        self.assertEqual(again.machine_role, "A")
        self.assertEqual(again.default_project, "alpha")
        self.assertEqual(again.project("alpha").last_synced_commit, "a" * 40)

    def test_file_is_private(self) -> None:
        cfg = config_mod.load()
        cfg.machine_role = "B"
        config_mod.save(cfg)
        # It records filesystem paths to every project.
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_no_temp_files_are_left_behind(self) -> None:
        cfg = config_mod.load()
        cfg.machine_role = "A"
        config_mod.save(cfg)
        leftovers = [p.name for p in self.root.iterdir() if p.name != "config.json"]
        self.assertEqual(leftovers, [])

    def test_concurrent_projects_are_not_clobbered(self) -> None:
        first = config_mod.load()
        first.project("alpha").last_synced_commit = "a" * 40
        config_mod.save(first)

        # A second run that never saw 'alpha' must not erase it.
        second = config_mod.load()
        stale = config_mod.Config(machine_role="A")
        stale.project("beta").last_synced_commit = "b" * 40
        config_mod.save(stale)

        merged = config_mod.load()
        self.assertIn("alpha", merged.projects)
        self.assertIn("beta", merged.projects)

    def test_unknown_keys_survive_a_save(self) -> None:
        self.path.write_text(
            json.dumps({"version": 1, "future_setting": "keep me"}), encoding="utf-8"
        )
        cfg = config_mod.load()
        config_mod.save(cfg)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw.get("future_setting"), "keep me")

    def test_shorthand_project_mapping_is_accepted(self) -> None:
        self.path.write_text(
            json.dumps({"version": 1, "projects": {"alpha": "c" * 40}}), encoding="utf-8"
        )
        cfg = config_mod.load()
        self.assertEqual(cfg.project("alpha").last_synced_commit, "c" * 40)

    def test_corrupt_file_raises_a_helpful_error(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(config_mod.ConfigError) as caught:
            config_mod.load()
        self.assertIn("full resync", str(caught.exception))


class ExcludePatterns(ConfigBase):
    def test_a_fresh_config_seeds_the_default_patterns(self) -> None:
        cfg = config_mod.load()
        self.assertEqual(cfg.exclude_patterns, config_mod.DEFAULT_EXCLUDE_PATTERNS)

    def test_an_existing_config_without_the_key_also_gets_the_default(self) -> None:
        # Simulates a config saved by a version of git-air-sync before this
        # feature existed — no migration code should be needed for this.
        self.path.write_text(json.dumps({"version": 1, "machine_role": "A"}), encoding="utf-8")
        cfg = config_mod.load()
        self.assertEqual(cfg.exclude_patterns, config_mod.DEFAULT_EXCLUDE_PATTERNS)

    def test_effective_patterns_are_the_union_of_global_and_project(self) -> None:
        cfg = config_mod.load()
        cfg.exclude_patterns = ["CLAUDE.md"]
        cfg.project("alpha").exclude_patterns = ["secrets.local.json"]

        effective = cfg.effective_exclude_patterns("alpha")
        self.assertEqual(effective, ["CLAUDE.md", "secrets.local.json"])
        # A project with no additions just gets the global list.
        self.assertEqual(cfg.effective_exclude_patterns("beta"), ["CLAUDE.md"])

    def test_effective_patterns_are_deduped(self) -> None:
        cfg = config_mod.load()
        cfg.exclude_patterns = ["CLAUDE.md"]
        cfg.project("alpha").exclude_patterns = ["CLAUDE.md", "extra.txt"]
        self.assertEqual(cfg.effective_exclude_patterns("alpha"), ["CLAUDE.md", "extra.txt"])

    def test_exclude_patterns_round_trip(self) -> None:
        cfg = config_mod.load()
        cfg.exclude_patterns = ["CLAUDE.md", ".claude/**"]
        cfg.project("alpha").exclude_patterns = ["extra.txt"]
        config_mod.save(cfg)

        again = config_mod.load()
        self.assertEqual(again.exclude_patterns, ["CLAUDE.md", ".claude/**"])
        self.assertEqual(again.project("alpha").exclude_patterns, ["extra.txt"])


class Validation(ConfigBase):
    def test_projects_root_must_exist(self) -> None:
        ok, reason = config_mod.validate_projects_root(str(self.root / "nope"))
        self.assertFalse(ok)
        self.assertIn("does not exist", reason)

    def test_projects_root_must_be_a_directory(self) -> None:
        target = self.root / "file.txt"
        target.write_text("x", encoding="utf-8")
        ok, _ = config_mod.validate_projects_root(str(target))
        self.assertFalse(ok)

    def test_existing_directory_is_accepted(self) -> None:
        ok, _ = config_mod.validate_projects_root(str(self.root))
        self.assertTrue(ok)

    def test_creatable_output_dir_is_accepted(self) -> None:
        ok, _ = config_mod.validate_writable_dir(str(self.root / "new-folder"))
        self.assertTrue(ok)

    def test_output_dir_with_missing_parent_is_rejected(self) -> None:
        ok, _ = config_mod.validate_writable_dir(str(self.root / "a" / "b" / "c"))
        self.assertFalse(ok)


class Paths(ConfigBase):
    def test_role_labels(self) -> None:
        cfg = config_mod.Config(machine_role="A")
        self.assertIn("COMPUTER A", cfg.role_label)
        cfg.machine_role = "B"
        self.assertIn("COMPUTER B", cfg.role_label)
        cfg.machine_role = None
        self.assertEqual(cfg.role_label, "UNCONFIGURED")

    def test_project_path_falls_back_to_the_root(self) -> None:
        repo = self.root / "alpha"
        repo.mkdir()
        cfg = config_mod.Config(projects_root=str(self.root))
        self.assertEqual(cfg.project_path("alpha"), repo)

    def test_explicit_project_path_wins(self) -> None:
        cfg = config_mod.Config(projects_root=str(self.root))
        cfg.project("alpha").path = "/somewhere/else"
        self.assertEqual(cfg.project_path("alpha"), Path("/somewhere/else"))


if __name__ == "__main__":
    unittest.main()
