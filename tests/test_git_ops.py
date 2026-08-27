"""git_ops against real throwaway repositories. No network, no mocks."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from git_air_sync.core import git_ops as git

from .support import commit, init_repo, run


class GitOpsBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.repo = init_repo(self.root / "repo")
        self.first = commit(self.repo, "one", "one.txt")
        self.second = commit(self.repo, "two", "two.txt")


class Inspection(GitOpsBase):
    def test_repo_detection(self) -> None:
        self.assertTrue(git.is_git_repo(self.repo))
        plain = self.root / "plain"
        plain.mkdir()
        self.assertFalse(git.is_git_repo(plain))

    def test_current_branch(self) -> None:
        self.assertEqual(git.current_branch(self.repo), "main")

    def test_detached_head_returns_none(self) -> None:
        run(["git", "checkout", "-q", "--detach", self.first], self.repo)
        self.assertIsNone(git.current_branch(self.repo))

    def test_empty_repo_has_no_commits(self) -> None:
        empty = init_repo(self.root / "empty")
        self.assertFalse(git.has_commits(empty))
        self.assertTrue(git.has_commits(self.repo))

    def test_rev_exists(self) -> None:
        self.assertTrue(git.rev_exists(self.repo, self.first))
        self.assertFalse(git.rev_exists(self.repo, "0" * 40))

    def test_is_ancestor(self) -> None:
        self.assertTrue(git.is_ancestor(self.repo, self.first, self.second))
        self.assertFalse(git.is_ancestor(self.repo, self.second, self.first))

    def test_count_commits(self) -> None:
        self.assertEqual(git.count_commits(self.repo, f"{self.first}..main"), 1)
        self.assertEqual(git.count_commits(self.repo, f"{self.second}..main"), 0)

    def test_list_commits_survives_awkward_subjects(self) -> None:
        nasty = 'subject with "quotes", | pipes and \x1f-ish text'
        commit(self.repo, nasty, "three.txt")
        commits = git.list_commits(self.repo, "main")
        self.assertEqual(commits[0].subject, nasty)
        self.assertEqual(len(commits), 3)

    def test_list_commits_respects_limit(self) -> None:
        self.assertEqual(len(git.list_commits(self.repo, "main", limit=1)), 1)

    def test_changed_files(self) -> None:
        changes = git.changed_files(self.repo, self.first, self.second)
        self.assertEqual([c.path for c in changes], ["two.txt"])
        self.assertEqual(changes[0].status, "A")

    def test_changed_files_handles_renames(self) -> None:
        run(["git", "mv", "two.txt", "renamed.txt"], self.repo)
        run(["git", "commit", "-q", "-m", "rename"], self.repo)
        changes = git.changed_files(self.repo, self.second, "HEAD")
        statuses = {c.status for c in changes}
        # Either a rename pair or an add+delete, depending on git's detection.
        self.assertTrue(statuses <= {"R", "A", "D"})
        if "R" in statuses:
            rename = next(c for c in changes if c.status == "R")
            self.assertEqual(rename.old_path, "two.txt")
            self.assertEqual(rename.path, "renamed.txt")

    def test_working_tree_status(self) -> None:
        clean = git.working_tree_status(self.repo)
        self.assertFalse(clean.dirty)
        self.assertEqual(clean.branch, "main")

        (self.repo / "one.txt").write_text("changed\n", encoding="utf-8")
        (self.repo / "new.txt").write_text("new\n", encoding="utf-8")
        dirty = git.working_tree_status(self.repo)
        self.assertTrue(dirty.dirty)
        self.assertEqual(dirty.unstaged, 1)
        self.assertEqual(dirty.untracked, 1)

    def test_discover_repos(self) -> None:
        init_repo(self.root / "nested" / "other")
        found = {p.name for p in git.discover_repos(self.root)}
        self.assertIn("repo", found)
        self.assertIn("other", found)


class PatchSeries(GitOpsBase):
    def test_format_patch_writes_a_nonempty_file(self) -> None:
        patch = self.root / "full.patch"
        git.format_patch(self.repo, patch, ["--root", "main"])
        self.assertTrue(patch.is_file())
        self.assertGreater(patch.stat().st_size, 0)
        self.assertIn("Subject:", patch.read_text(encoding="utf-8"))

    def test_am_applies_a_clean_series(self) -> None:
        clone = self.root / "clone"
        run(["git", "clone", "-q", str(self.repo), str(clone)])
        run(["git", "remote", "remove", "origin"], clone)

        commit(self.repo, "three", "three.txt")
        patch = self.root / "inc.patch"
        git.format_patch(self.repo, patch, [f"{self.second}..main"])

        result = git.am_apply(clone, patch, three_way=True)
        self.assertTrue(result.ok)
        # Applied for real, but under a NEW hash — `git am` sets its own committer
        # date, so byte-identical content still doesn't reproduce the original sha.
        self.assertNotEqual(git.resolve_sha(clone, "HEAD"), git.resolve_sha(self.repo, "HEAD"))
        self.assertEqual((clone / "three.txt").read_text(), "three\n")
        self.assertEqual(git.list_commits(clone, "HEAD", limit=1)[0].subject, "three")

    def test_am_applies_even_when_the_base_object_is_missing(self) -> None:
        """The core fix: B never needs the prerequisite commit to exist as an
        object — only matching file content, exactly the case a rebase/amend on B
        used to break."""
        commit(self.repo, "three", "two.txt", body="two\nthree\n")
        patch = self.root / "inc.patch"
        git.format_patch(self.repo, patch, [f"{self.second}..main"])

        # Equivalent content, but a genuinely different commit graph — as if the
        # user amended history on B. `self.second`'s commit object does not exist
        # here at all.
        other = init_repo(self.root / "other")
        commit(other, "one (redone)", "one.txt", body="one\n")
        commit(other, "two (redone)", "two.txt", body="two\n")
        self.assertFalse(git.rev_exists(other, self.second))

        result = git.am_apply(other, patch, three_way=True)
        self.assertTrue(result.ok)
        self.assertEqual((other / "two.txt").read_text(), "two\nthree\n")

    def test_bootstrap_onto_a_brand_new_repo(self) -> None:
        commit(self.repo, "three", "three.txt")
        full = self.root / "full.patch"
        git.format_patch(self.repo, full, ["--root", "main"])

        dest = self.root / "bootstrapped"
        git.init_repo(dest, "main")
        result = git.am_apply(dest, full, three_way=True)
        self.assertTrue(result.ok)
        self.assertEqual(
            set(p for p in run(["git", "ls-files"], dest).splitlines()),
            {"one.txt", "two.txt", "three.txt"},
        )

    def test_conflict_leaves_am_in_progress_and_abort_rolls_back(self) -> None:
        run(["git", "checkout", "-q", "-b", "side", self.first], self.repo)
        commit(self.repo, "side", "shared.txt", body="side\n")
        patch = self.root / "side.patch"
        git.format_patch(self.repo, patch, [f"{self.first}..side"])

        run(["git", "checkout", "-q", "main"], self.repo)
        commit(self.repo, "main", "shared.txt", body="main\n")
        original_tip = git.resolve_sha(self.repo, "main")

        result = git.am_apply(self.repo, patch, three_way=True)
        self.assertFalse(result.ok)
        self.assertTrue(git.am_in_progress(self.repo))
        self.assertIn("shared.txt", git.conflicted_files(self.repo))

        self.assertTrue(git.abort_am(self.repo))
        self.assertFalse(git.am_in_progress(self.repo))
        self.assertEqual(git.resolve_sha(self.repo, "main"), original_tip)

    def test_worktree_dry_run_does_not_touch_the_real_branch(self) -> None:
        run(["git", "checkout", "-q", "-b", "side", self.first], self.repo)
        commit(self.repo, "side", "shared.txt", body="side\n")
        patch = self.root / "side.patch"
        git.format_patch(self.repo, patch, [f"{self.first}..side"])

        run(["git", "checkout", "-q", "main"], self.repo)
        commit(self.repo, "main", "shared.txt", body="main\n")
        original_tip = git.resolve_sha(self.repo, "main")

        worktree = self.root / "wt"
        git.add_worktree(self.repo, worktree, "main")
        try:
            result = git.am_apply(worktree, patch, three_way=True)
            self.assertFalse(result.ok)
            self.assertTrue(git.am_in_progress(worktree))
            git.abort_am(worktree)
        finally:
            git.remove_worktree(self.repo, worktree)

        self.assertEqual(git.resolve_sha(self.repo, "main"), original_tip)
        self.assertFalse(git.working_tree_status(self.repo).dirty)


class Runner(unittest.TestCase):
    def test_failure_raises_with_stderr_attached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(git.GitError) as caught:
                git.run_git(["-C", tmp, "rev-parse", "HEAD"])
            self.assertTrue(str(caught.exception))

    def test_check_false_returns_the_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = git.run_git(["-C", tmp, "rev-parse", "HEAD"], check=False)
            self.assertFalse(result.ok)


if __name__ == "__main__":
    unittest.main()
