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


class Bundles(GitOpsBase):
    def test_full_bundle_records_complete_history(self) -> None:
        bundle = self.root / "full.bundle"
        git.create_bundle(self.repo, bundle, ["main"])
        self.assertTrue(bundle.is_file())

        verification = git.verify_bundle(self.repo, bundle)
        self.assertTrue(verification.ok)
        self.assertTrue(verification.complete_history)
        self.assertIn("refs/heads/main", verification.refs)

    def test_incremental_bundle_verifies_where_the_base_exists(self) -> None:
        bundle = self.root / "inc.bundle"
        git.create_bundle(self.repo, bundle, [f"{self.first}..main"])
        self.assertTrue(git.verify_bundle(self.repo, bundle).ok)

    def test_missing_prerequisite_is_detected_and_parsed(self) -> None:
        """The core mechanism for 'B is missing an earlier package'."""
        bundle = self.root / "inc.bundle"
        git.create_bundle(self.repo, bundle, [f"{self.first}..main"])

        # A fresh repo that has never seen `first`.
        other = init_repo(self.root / "other")
        commit(other, "unrelated", "unrelated.txt")

        verification = git.verify_bundle(other, bundle)
        self.assertFalse(verification.ok)
        self.assertIn(self.first, verification.missing_prereqs)

    def test_garbage_is_not_mistaken_for_a_bundle(self) -> None:
        bogus = self.root / "bogus.bundle"
        bogus.write_bytes(b"definitely not a bundle")
        verification = git.verify_bundle(self.repo, bogus)
        self.assertFalse(verification.ok)
        self.assertTrue(verification.not_a_bundle)

    def test_fetch_and_merge_from_a_bundle(self) -> None:
        clone = self.root / "clone"
        run(["git", "clone", "-q", str(self.repo), str(clone)])
        run(["git", "remote", "remove", "origin"], clone)

        third = commit(self.repo, "three", "three.txt")
        bundle = self.root / "inc.bundle"
        git.create_bundle(self.repo, bundle, [f"{self.second}..main"])

        git.fetch_from_bundle(clone, bundle, "refs/heads/main", git.INCOMING_REF)
        self.assertTrue(git.ref_exists(clone, git.INCOMING_REF))

        outcome, _ = git.merge_ref(clone, git.INCOMING_REF, message="merge")
        self.assertIn(
            outcome, (git.MergeOutcome.FAST_FORWARD, git.MergeOutcome.MERGED)
        )
        self.assertEqual(git.resolve_sha(clone, "HEAD"), third)

    def test_incoming_ref_is_hidden_from_git_branch(self) -> None:
        """refs/air-sync/* must not pollute the user's branch list."""
        clone = self.root / "clone"
        run(["git", "clone", "-q", str(self.repo), str(clone)])
        commit(self.repo, "three", "three.txt")
        bundle = self.root / "inc.bundle"
        git.create_bundle(self.repo, bundle, [f"{self.second}..main"])
        git.fetch_from_bundle(clone, bundle, "refs/heads/main", git.INCOMING_REF)

        branches = run(["git", "branch"], clone)
        self.assertNotIn("air-sync", branches)

    def test_a_branch_named_air_sync_does_not_collide(self) -> None:
        """The reason for refs/air-sync/ over refs/heads/air-sync/."""
        run(["git", "branch", "air-sync"], self.repo)
        clone = self.root / "clone"
        run(["git", "clone", "-q", str(self.repo), str(clone)])
        commit(self.repo, "three", "three.txt")
        bundle = self.root / "inc.bundle"
        git.create_bundle(self.repo, bundle, [f"{self.second}..main"])
        # Would raise a directory/file ref conflict under refs/heads/air-sync/.
        git.fetch_from_bundle(clone, bundle, "refs/heads/main", git.INCOMING_REF)
        self.assertTrue(git.ref_exists(clone, git.INCOMING_REF))

    def test_clone_from_bundle_removes_origin(self) -> None:
        bundle = self.root / "full.bundle"
        git.create_bundle(self.repo, bundle, ["main"])
        dest = self.root / "bootstrapped"
        git.clone_from_bundle(bundle, dest, "main")
        self.assertTrue((dest / ".git").is_dir())
        self.assertEqual(run(["git", "remote"], dest).strip(), "")


class MergePreviews(GitOpsBase):
    def test_clean_merge_is_predicted(self) -> None:
        run(["git", "checkout", "-q", "-b", "side"], self.repo)
        commit(self.repo, "side change", "side.txt")
        run(["git", "checkout", "-q", "main"], self.repo)

        preview = git.preview_merge(self.repo, "main", "side")
        self.assertTrue(preview.clean)
        self.assertEqual(preview.conflicts, [])

    def test_conflict_is_predicted_without_touching_the_worktree(self) -> None:
        run(["git", "checkout", "-q", "-b", "side"], self.repo)
        commit(self.repo, "side", "shared.txt", body="side\n")
        run(["git", "checkout", "-q", "main"], self.repo)
        commit(self.repo, "main", "shared.txt", body="main\n")

        preview = git.preview_merge(self.repo, "main", "side")
        self.assertFalse(preview.clean)
        self.assertIn("shared.txt", preview.conflicts)
        # The working tree must be untouched by a preview.
        self.assertFalse(git.working_tree_status(self.repo).dirty)
        self.assertFalse(git.merge_in_progress(self.repo))


class Conflicts(GitOpsBase):
    def test_conflicted_files_are_listed(self) -> None:
        run(["git", "checkout", "-q", "-b", "side"], self.repo)
        commit(self.repo, "side", "shared.txt", body="side\n")
        run(["git", "checkout", "-q", "main"], self.repo)
        commit(self.repo, "main", "shared.txt", body="main\n")

        outcome, _ = git.merge_ref(self.repo, "side", message="merge")
        self.assertIs(outcome, git.MergeOutcome.CONFLICT)
        self.assertIn("shared.txt", git.conflicted_files(self.repo))
        self.assertTrue(git.merge_in_progress(self.repo))

        self.assertTrue(git.abort_merge(self.repo))
        self.assertFalse(git.merge_in_progress(self.repo))


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
