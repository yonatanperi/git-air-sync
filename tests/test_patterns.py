"""Gitignore-style exclude pattern matching."""

from __future__ import annotations

import unittest

from git_air_sync.core import patterns as pat


class BareNameMatchesAnyDepth(unittest.TestCase):
    def test_matches_at_root(self) -> None:
        self.assertTrue(pat.compile_pattern("CLAUDE.md").match("CLAUDE.md"))

    def test_matches_nested(self) -> None:
        self.assertTrue(pat.compile_pattern("CLAUDE.md").match("sub/dir/CLAUDE.md"))

    def test_does_not_match_a_different_name(self) -> None:
        self.assertFalse(pat.compile_pattern("CLAUDE.md").match("NOTCLAUDE.md"))
        self.assertFalse(pat.compile_pattern("CLAUDE.md").match("CLAUDE.md.bak"))


class SlashAnchoredPatterns(unittest.TestCase):
    def test_anchored_to_root(self) -> None:
        rx = pat.compile_pattern("docs/CLAUDE.md")
        self.assertTrue(rx.match("docs/CLAUDE.md"))
        self.assertFalse(rx.match("other/docs/CLAUDE.md"))
        self.assertFalse(rx.match("CLAUDE.md"))

    def test_single_star_does_not_cross_slash(self) -> None:
        rx = pat.compile_pattern("docs/*.local.md")
        self.assertTrue(rx.match("docs/notes.local.md"))
        self.assertFalse(rx.match("docs/sub/notes.local.md"))

    def test_double_star_crosses_slash(self) -> None:
        rx = pat.compile_pattern(".claude/**")
        self.assertTrue(rx.match(".claude/settings.json"))
        self.assertTrue(rx.match(".claude/nested/deep/file.json"))
        self.assertFalse(rx.match(".claude"))
        self.assertFalse(rx.match("other/.claude/settings.json"))


class TrailingSlash(unittest.TestCase):
    def test_directory_marker_behaves_like_double_star(self) -> None:
        rx = pat.compile_pattern(".claude/")
        self.assertTrue(rx.match(".claude/settings.json"))
        self.assertTrue(rx.match(".claude/nested/file.json"))


class Pathspec(unittest.TestCase):
    def test_bare_name_uses_any_depth_glob(self) -> None:
        self.assertEqual(pat.to_pathspec("CLAUDE.md"), ":(exclude,glob)**/CLAUDE.md")

    def test_slash_pattern_is_anchored_to_top(self) -> None:
        self.assertEqual(pat.to_pathspec(".claude/**"), ":(exclude,glob,top).claude/**")


class Utilities(unittest.TestCase):
    def test_matches_any_returns_the_matching_pattern(self) -> None:
        self.assertEqual(
            pat.matches_any("sub/CLAUDE.md", ["other.txt", "CLAUDE.md"]), "CLAUDE.md"
        )
        self.assertIsNone(pat.matches_any("real.py", ["CLAUDE.md", ".claude/**"]))

    def test_dedupe_preserves_order_and_strips_blanks(self) -> None:
        self.assertEqual(
            pat.dedupe(["CLAUDE.md", " CLAUDE.md ", "", ".claude/**", "CLAUDE.md"]),
            ["CLAUDE.md", ".claude/**"],
        )


if __name__ == "__main__":
    unittest.main()
