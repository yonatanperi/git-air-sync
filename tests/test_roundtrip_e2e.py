"""End-to-end A -> B, on one machine, using real git and the real orchestration.

``AIR_SYNC_CONFIG`` is what makes this possible: two config files stand in for two
machines. Nothing here is mocked except the human.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from git_air_sync import config as config_mod
from git_air_sync.core import git_ops as git, sync
from git_air_sync.errors import NothingToDo, PayloadError


def file_text(repo: Path, name: str) -> str:
    return (repo / name).read_text(encoding="utf-8")

from .support import ScriptedReporter, commit, head, init_repo, log_subjects, run


class RoundTripBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        self.a_root = self.root / "A"
        self.b_root = self.root / "B"
        self.drop = self.root / "drop"
        for directory in (self.a_root, self.b_root, self.drop):
            directory.mkdir(parents=True)

        self.a_repo = init_repo(self.a_root / "alpha")
        commit(self.a_repo, "one", "one.txt")
        commit(self.a_repo, "two", "two.txt")

        # B starts as a clone, then loses its remote — nothing may reach the network.
        run(["git", "clone", "-q", str(self.a_repo), str(self.b_root / "alpha")])
        self.b_repo = self.b_root / "alpha"
        run(["git", "remote", "remove", "origin"], self.b_repo)

        self.a_config = self.root / "a.json"
        self.b_config = self.root / "b.json"
        self._original = os.environ.get("AIR_SYNC_CONFIG")
        self.addCleanup(self._restore_env)

        # B was cloned from A, so A already knows where B stands. Without this the
        # first export would be a full-history one and every incremental assertion
        # below would be testing the wrong thing.
        self.clone_point = head(self.a_repo)
        seeded = self._cfg("A")
        state = seeded.project("alpha")
        state.path = str(self.a_repo)
        state.last_synced_commit = self.clone_point
        state.last_synced_branch = "main"
        config_mod.save(seeded)

    def _restore_env(self) -> None:
        if self._original is None:
            os.environ.pop("AIR_SYNC_CONFIG", None)
        else:
            os.environ["AIR_SYNC_CONFIG"] = self._original

    def _cfg(self, which: str) -> config_mod.Config:
        os.environ["AIR_SYNC_CONFIG"] = str(
            self.a_config if which == "A" else self.b_config
        )
        cfg = config_mod.load()
        cfg.machine_role = which
        cfg.projects_root = str(self.a_root if which == "A" else self.b_root)
        cfg.drop_folder = str(self.drop)
        return cfg

    def _export(self, **kwargs) -> sync.ExportResult:
        cfg = self._cfg("A")
        state = cfg.project("alpha")
        state.path = str(self.a_repo)
        reporter = kwargs.pop("reporter", ScriptedReporter())
        result = sync.export_project(
            self.a_repo, "alpha", state, self.drop, reporter,
            assume_yes=kwargs.pop("assume_yes", True), **kwargs,
        )
        state.last_synced_commit = result.plan.head
        state.last_synced_branch = result.plan.branch
        config_mod.save(cfg)
        return result

    def _import(self, docx: Path, **kwargs) -> sync.ImportResult:
        cfg = self._cfg("B")
        cfg.project("alpha").path = str(self.b_repo)
        reporter = kwargs.pop("reporter", ScriptedReporter())
        return sync.import_document(
            docx, cfg, reporter, assume_yes=kwargs.pop("assume_yes", True), **kwargs
        )


class HappyPath(RoundTripBase):
    def test_new_commits_reach_the_other_machine(self) -> None:
        commit(self.a_repo, "three", "three.txt")

        result = self._export()
        self.assertTrue(result.path.is_file())
        self.assertEqual(result.plan.mode, "incremental")
        self.assertEqual(result.plan.commit_count, 1)

        imported = self._import(result.path)
        self.assertTrue(imported.applied)
        # Commit hashes never match across machines — `git am` always creates a new
        # object (different committer date) even for byte-identical content — so the
        # regression net here is content arriving intact, not hash equality.
        self.assertEqual(file_text(self.b_repo, "three.txt"), file_text(self.a_repo, "three.txt"))
        self.assertIn("three", log_subjects(self.b_repo))

    def test_document_size_matches_the_estimate(self) -> None:
        # Incompressible content, so the patch series is large enough that the fixed
        # OOXML overhead stops dominating and the asymptotic ratio applies.
        import os as _os

        commit(
            self.a_repo, "three", "big.bin", body=_os.urandom(300_000).hex()
        )
        result = self._export()

        from git_air_sync.core.codec import estimate_docx_size

        estimated = estimate_docx_size(result.patch_bytes)
        self.assertLess(
            abs(result.docx_bytes - estimated) / result.docx_bytes,
            0.10,
            f"estimate {estimated} vs actual {result.docx_bytes}",
        )

    def test_second_export_is_incremental_and_smaller(self) -> None:
        commit(self.a_repo, "three", "three.txt", body="y" * 50000)
        first = self._export(force_full=True)
        self._import(first.path)

        commit(self.a_repo, "four", "four.txt")
        second = self._export()
        self.assertEqual(second.plan.mode, "incremental")
        self.assertLess(second.patch_bytes, first.patch_bytes)

        self._import(second.path)
        self.assertEqual(file_text(self.b_repo, "four.txt"), file_text(self.a_repo, "four.txt"))
        self.assertEqual(set(log_subjects(self.b_repo)), set(log_subjects(self.a_repo)))

    def test_exporting_with_nothing_new_is_not_an_error(self) -> None:
        commit(self.a_repo, "three", "three.txt")
        self._export()
        with self.assertRaises(NothingToDo):
            self._export()

    def test_author_survives_the_gap_under_a_new_hash(self) -> None:
        commit(self.a_repo, "three", "three.txt")
        imported = self._import(self._export().path)

        fmt = "--format=%an|%ae|%s"
        a_meta = run(["git", "log", "-1", fmt, "main"], self.a_repo).strip()
        b_meta = run(["git", "log", "-1", fmt, imported.local_head], self.b_repo).strip()
        self.assertEqual(a_meta, b_meta)
        # The whole point of the patch transport: B's commit is a different object.
        self.assertNotEqual(imported.local_head, head(self.a_repo))

    def test_no_scratch_directories_survive(self) -> None:
        commit(self.a_repo, "three", "three.txt")
        self._import(self._export().path)
        leftovers = list(Path(tempfile.gettempdir()).glob(sync.SCRATCH_PREFIX + "*"))
        self.assertEqual(leftovers, [])

    def test_no_worktrees_survive(self) -> None:
        commit(self.a_repo, "three", "three.txt")
        self._import(self._export().path)
        worktrees = run(["git", "worktree", "list"], self.b_repo).strip().splitlines()
        self.assertEqual(len(worktrees), 1)  # just the main working tree


class Bootstrap(RoundTripBase):
    def test_full_export_creates_a_missing_repo(self) -> None:
        import shutil

        shutil.rmtree(self.b_repo)
        commit(self.a_repo, "three", "three.txt")

        result = self._export(force_full=True)
        imported = self._import(result.path)

        self.assertTrue(imported.bootstrapped)
        self.assertTrue((self.b_repo / ".git").is_dir())
        self.assertEqual(set(log_subjects(self.b_repo)), set(log_subjects(self.a_repo)))
        self.assertEqual(file_text(self.b_repo, "three.txt"), file_text(self.a_repo, "three.txt"))
        # The temporary patch file must not survive as a remote.
        remotes = run(["git", "remote"], self.b_repo).strip()
        self.assertEqual(remotes, "")


class Failures(RoundTripBase):
    def test_a_skipped_export_still_applies_via_patch(self) -> None:
        """The bug that motivated the patch transport: under the old git-bundle
        mechanism, importing a package whose base commit was never delivered (or,
        equivalently, no longer exists after a rebase/amend on B) was an unrecoverable
        hard failure. A patch series doesn't need that commit to exist as an object —
        only for its content to still be there — so this now just works."""
        commit(self.a_repo, "three", "three.txt")
        first = self._export()  # base = "two", never delivered to B

        commit(self.a_repo, "four", "four.txt")
        second = self._export()  # base = "three", which B does not have

        imported = self._import(second.path)
        self.assertTrue(imported.applied)
        self.assertEqual(file_text(self.b_repo, "four.txt"), file_text(self.a_repo, "four.txt"))
        self.assertTrue(first.path.is_file())

    def test_rebased_local_history_surfaces_as_a_conflict_not_a_hard_error(self) -> None:
        """Same bug, via the scenario the user actually hit: B amends the commit A
        thinks it last synced, so that exact commit object no longer exists on B at
        all. A genuine content conflict is still possible and should surface
        normally — it must not come back as an unrecoverable PayloadError."""
        commit(self.a_repo, "three", "shared.txt", body="from A\n")
        first = self._export()
        self._import(first.path)

        # B rewrites both the content and the identity of the commit A last synced.
        (self.b_repo / "shared.txt").write_text("from B, amended\n", encoding="utf-8")
        run(["git", "add", "shared.txt"], self.b_repo)
        run(["git", "commit", "--amend", "-q", "--no-edit"], self.b_repo)
        self.assertNotEqual(head(self.b_repo), first.plan.head)

        commit(self.a_repo, "four", "shared.txt", body="from A, again\n")
        second = self._export()

        with self.assertRaises(sync.MergeConflictDetail) as caught:
            self._import(second.path)
        self.assertIn("shared.txt", caught.exception.conflicts)

    def test_corrupted_document_is_explained(self) -> None:
        import zipfile

        commit(self.a_repo, "three", "three.txt")
        docx = self._export().path

        with zipfile.ZipFile(docx) as zf:
            parts = {n: zf.read(n) for n in zf.namelist()}
        xml = parts["word/document.xml"].decode()
        parts["word/document.xml"] = xml.replace(
            'xml:space="preserve">', 'xml:space="preserve">1 ', 1
        ).encode()
        with zipfile.ZipFile(docx, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in parts.items():
                zf.writestr(name, data)

        with self.assertRaises(PayloadError):
            self._import(docx)

        # A failed import must not advance the recorded position.
        cfg = self._cfg("B")
        self.assertIsNone(cfg.project("alpha").last_synced_commit)

    def test_wrong_file_is_rejected(self) -> None:
        bogus = self.drop / "holiday-photos.docx"
        bogus.write_bytes(b"PK\x03\x04 definitely not ours")
        with self.assertRaises(PayloadError):
            self._import(bogus)


class Conflicts(RoundTripBase):
    def test_conflict_stops_and_records_state(self) -> None:
        commit(self.a_repo, "three", "shared.txt", body="from A\n")
        commit(self.b_repo, "local", "shared.txt", body="from B\n")

        docx = self._export().path

        with self.assertRaises(sync.MergeConflictDetail) as caught:
            self._import(docx)

        conflict = caught.exception
        self.assertIn("shared.txt", conflict.conflicts)

        cfg = self._cfg("B")
        state = cfg.project("alpha")
        # The position must NOT advance while the merge is unfinished.
        self.assertIsNone(state.last_synced_commit)
        self.assertIsNotNone(state.pending_conflict)

    def test_resolve_finalises_after_the_user_stages_and_continues(self) -> None:
        commit(self.a_repo, "three", "shared.txt", body="from A\n")
        commit(self.b_repo, "local", "shared.txt", body="from B\n")
        docx = self._export().path

        with self.assertRaises(sync.MergeConflictDetail):
            self._import(docx)
        self.assertTrue(git.am_in_progress(self.b_repo))

        # Stand in for the human resolving the conflict — 'git am --continue' (not
        # 'git commit') is what finishes an am, and finalize_resolution runs it.
        (self.b_repo / "shared.txt").write_text("merged by hand\n", encoding="utf-8")
        run(["git", "add", "shared.txt"], self.b_repo)

        cfg = self._cfg("B")
        self.assertTrue(sync.finalize_resolution(self.b_repo, "alpha", cfg))

        state = config_mod.load().project("alpha")
        self.assertIsNotNone(state.last_synced_commit)
        self.assertIsNotNone(state.last_import_head)
        self.assertIsNone(state.pending_conflict)
        self.assertFalse(git.am_in_progress(self.b_repo))
        self.assertEqual(file_text(self.b_repo, "shared.txt"), "merged by hand\n")

    def test_resolve_refuses_while_conflicts_remain(self) -> None:
        from git_air_sync.errors import MergeConflict

        commit(self.a_repo, "three", "shared.txt", body="from A\n")
        commit(self.b_repo, "local", "shared.txt", body="from B\n")
        docx = self._export().path

        with self.assertRaises(sync.MergeConflictDetail):
            self._import(docx)

        cfg = self._cfg("B")
        with self.assertRaises(MergeConflict):
            sync.finalize_resolution(self.b_repo, "alpha", cfg)


class DirtyTree(RoundTripBase):
    def test_uncommitted_changes_prompt_and_are_excluded(self) -> None:
        commit(self.a_repo, "three", "three.txt")
        (self.a_repo / "scratch.txt").write_text("not committed\n", encoding="utf-8")

        reporter = ScriptedReporter(confirm=True)
        result = self._export(assume_yes=False, reporter=reporter)

        titles = [title for title, _ in reporter.warnings]
        self.assertIn("Uncommitted changes", titles)

        self._import(result.path)
        self.assertFalse((self.b_repo / "scratch.txt").exists())

    def test_declining_aborts_the_export(self) -> None:
        from git_air_sync.errors import UserAbort

        commit(self.a_repo, "three", "three.txt")
        (self.a_repo / "scratch.txt").write_text("not committed\n", encoding="utf-8")

        with self.assertRaises(UserAbort):
            self._export(assume_yes=False, reporter=ScriptedReporter(confirm=False))


if __name__ == "__main__":
    unittest.main()
