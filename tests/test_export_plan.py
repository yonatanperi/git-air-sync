"""The export decision table.

This is the densest logic in the tool — it decides what actually gets bundled when
history has moved under you — so it gets direct coverage rather than only being
exercised through the end-to-end path.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from git_air_sync.config import ProjectState
from git_air_sync.core import git_ops as git, sync
from git_air_sync.errors import NothingToDo, UserAbort

from .support import ScriptedReporter, commit, init_repo, run


class PlanBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.repo = init_repo(self.root / "repo")
        self.first = commit(self.repo, "one", "one.txt")
        self.second = commit(self.repo, "two", "two.txt")

    def plan(self, state=None, **kwargs) -> sync.ExportPlan:
        reporter = kwargs.pop("reporter", ScriptedReporter())
        return sync.resolve_export_plan(
            self.repo, state or ProjectState(), reporter, **kwargs
        )


class NormalCases(PlanBase):
    def test_incremental_when_base_is_an_ancestor(self) -> None:
        third = commit(self.repo, "three", "three.txt")
        plan = self.plan(ProjectState(last_synced_commit=self.second))
        self.assertEqual(plan.mode, "incremental")
        self.assertEqual(plan.base, self.second)
        self.assertEqual(plan.head, third)
        self.assertEqual(plan.commit_count, 1)
        self.assertEqual(plan.revs, [f"{self.second}..main"])

    def test_already_synced_raises_nothing_to_do(self) -> None:
        with self.assertRaises(NothingToDo):
            self.plan(ProjectState(last_synced_commit=self.second))

    def test_force_full_ignores_the_recorded_base(self) -> None:
        commit(self.repo, "three", "three.txt")
        plan = self.plan(ProjectState(last_synced_commit=self.second), force_full=True)
        self.assertEqual(plan.mode, "full")
        self.assertIsNone(plan.base)
        self.assertEqual(plan.commit_count, 3)

    def test_base_override_wins(self) -> None:
        commit(self.repo, "three", "three.txt")
        plan = self.plan(
            ProjectState(last_synced_commit=self.second), base_override=self.first
        )
        self.assertEqual(plan.base, self.first)
        self.assertEqual(plan.commit_count, 2)

    def test_export_refs_all_includes_branches_and_tags(self) -> None:
        commit(self.repo, "three", "three.txt")
        plan = self.plan(
            ProjectState(last_synced_commit=self.second), export_refs="all"
        )
        self.assertIn("--branches", plan.revs)
        self.assertIn("--tags", plan.revs)
        self.assertIn("--not", plan.revs)


class FirstSync(PlanBase):
    def test_prompts_and_full_is_the_default(self) -> None:
        reporter = ScriptedReporter()
        plan = self.plan(reporter=reporter)
        self.assertEqual(plan.mode, "full")
        self.assertTrue(any("never been synced" in q for q in reporter.questions))

    def test_assume_yes_takes_the_default_without_asking(self) -> None:
        """Regression: a scripted first export used to dead-end on the prompt."""
        reporter = ScriptedReporter()
        plan = self.plan(reporter=reporter, assume_yes=True)
        self.assertEqual(plan.mode, "full")
        self.assertEqual(reporter.questions, [])

    def test_user_can_pick_a_starting_commit(self) -> None:
        reporter = ScriptedReporter(
            options={"choice": "pick"}, commit_choice=self.first
        )
        plan = self.plan(reporter=reporter)
        self.assertEqual(plan.mode, "incremental")
        self.assertEqual(plan.base, self.first)

    def test_user_can_abort(self) -> None:
        with self.assertRaises(UserAbort):
            self.plan(reporter=ScriptedReporter(options={"choice": "abort"}))


class RecordedCommitVanished(PlanBase):
    """Amend, rebase, or gc removed the commit we thought we were synced at."""

    def test_falls_back_to_full_when_asked(self) -> None:
        state = ProjectState(last_synced_commit="0" * 40)
        reporter = ScriptedReporter(options={"choice": "full"})
        plan = self.plan(state, reporter=reporter)
        self.assertEqual(plan.mode, "full")
        self.assertTrue(any("no longer exists" in q for q in reporter.questions))

    def test_assume_yes_defaults_to_full_not_pick(self) -> None:
        # "pick" is the interactive default but is unscriptable, so --yes must
        # resolve to something that can actually complete.
        state = ProjectState(last_synced_commit="0" * 40)
        reporter = ScriptedReporter()
        plan = self.plan(state, reporter=reporter, assume_yes=True)
        self.assertEqual(plan.mode, "full")
        self.assertEqual(reporter.questions, [])

    def test_abort_is_honoured(self) -> None:
        with self.assertRaises(UserAbort):
            self.plan(
                ProjectState(last_synced_commit="0" * 40),
                reporter=ScriptedReporter(options={"choice": "abort"}),
            )


class DivergedHistory(PlanBase):
    def setUp(self) -> None:
        super().setUp()
        # Build a genuine fork: `side` is not an ancestor of `main`.
        run(["git", "checkout", "-q", "-b", "side", self.first], self.repo)
        self.side = commit(self.repo, "side work", "side.txt")
        run(["git", "checkout", "-q", "main"], self.repo)
        commit(self.repo, "main work", "main.txt")

    def test_offers_the_common_ancestor(self) -> None:
        reporter = ScriptedReporter(options={"choice": "common"})
        plan = self.plan(ProjectState(last_synced_commit=self.side), reporter=reporter)
        self.assertEqual(plan.base, self.first)
        self.assertTrue(any("diverged" in q for q in reporter.questions))

    def test_never_bundles_the_diverged_range_silently(self) -> None:
        reporter = ScriptedReporter(options={"choice": "full"})
        plan = self.plan(ProjectState(last_synced_commit=self.side), reporter=reporter)
        self.assertEqual(plan.mode, "full")
        self.assertNotEqual(plan.base, self.side)


class Refusals(PlanBase):
    def test_detached_head_is_refused(self) -> None:
        from git_air_sync.errors import EnvironmentError_

        run(["git", "checkout", "-q", "--detach", self.first], self.repo)
        with self.assertRaises(EnvironmentError_) as caught:
            self.plan()
        self.assertIn("detached", str(caught.exception))

    def test_empty_repository_is_refused(self) -> None:
        from git_air_sync.errors import EnvironmentError_

        empty = init_repo(self.root / "empty")
        with self.assertRaises(EnvironmentError_):
            sync.resolve_export_plan(empty, ProjectState(), ScriptedReporter())


if __name__ == "__main__":
    unittest.main()
