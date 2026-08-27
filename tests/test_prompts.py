"""Prompt fallbacks: the path Computer B actually takes."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from git_air_sync.cli import prompts
from git_air_sync.cli.prompts import Choice
from git_air_sync.errors import NonInteractiveError, UserAbort


def _tty(value: bool):
    return mock.patch("sys.stdin.isatty", return_value=value)


def _plain():
    return mock.patch.object(prompts.theme, "HAS_QUESTIONARY", False)


class NonInteractive(unittest.TestCase):
    """A prompt that hangs inside a script is the worst failure mode here."""

    def test_confirm_refuses_without_a_terminal(self) -> None:
        with _tty(False), self.assertRaises(NonInteractiveError) as caught:
            prompts.confirm("Proceed?", flag="--yes")
        self.assertIn("--yes", str(caught.exception))

    def test_select_refuses_without_a_terminal(self) -> None:
        with _tty(False), self.assertRaises(NonInteractiveError):
            prompts.select("Pick", [Choice(1, "a"), Choice(2, "b")], flag="PROJECT")

    def test_single_choice_needs_no_terminal(self) -> None:
        with _tty(False):
            self.assertEqual(prompts.select("Pick", [Choice("only", "only")]), "only")


class EndOfInput(unittest.TestCase):
    """Regression: a closed stdin used to escape as an unhandled EOFError."""

    def test_confirm_treats_eof_as_abort(self) -> None:
        with _tty(True), _plain(), mock.patch("builtins.input", side_effect=EOFError):
            with redirect_stdout(io.StringIO()), self.assertRaises(UserAbort):
                prompts.confirm("Proceed?")

    def test_select_treats_eof_as_abort(self) -> None:
        with _tty(True), _plain(), mock.patch("builtins.input", side_effect=EOFError):
            with redirect_stdout(io.StringIO()), self.assertRaises(UserAbort):
                prompts.select("Pick", [Choice(1, "a"), Choice(2, "b")])


class PlainConfirm(unittest.TestCase):
    def _confirm(self, typed: str, default: bool) -> bool:
        with _tty(True), _plain(), mock.patch("builtins.input", return_value=typed):
            with redirect_stdout(io.StringIO()):
                return prompts.confirm("Proceed?", default=default)

    def test_empty_input_takes_the_default(self) -> None:
        self.assertTrue(self._confirm("", True))
        self.assertFalse(self._confirm("", False))

    def test_yes_and_no_are_understood(self) -> None:
        for text in ("y", "Y", "yes", "YES"):
            self.assertTrue(self._confirm(text, False), text)
        for text in ("n", "N", "no", "NO"):
            self.assertFalse(self._confirm(text, True), text)

    def test_invalid_answer_reprompts(self) -> None:
        with _tty(True), _plain(), mock.patch(
            "builtins.input", side_effect=["maybe", "y"]
        ):
            with redirect_stdout(io.StringIO()) as out:
                self.assertTrue(prompts.confirm("Proceed?"))
        self.assertIn("answer y or n", out.getvalue())


class PlainSelect(unittest.TestCase):
    CHOICES = [
        Choice("alpha", "alpha"),
        Choice("beta", "beta", is_default=True),
        Choice("gamma", "gamma"),
    ]

    def _select(self, typed):
        side = typed if isinstance(typed, list) else [typed]
        with _tty(True), _plain(), mock.patch("builtins.input", side_effect=side):
            with redirect_stdout(io.StringIO()) as out:
                return prompts.select("Pick", self.CHOICES, default="beta"), out.getvalue()

    def test_number_selects(self) -> None:
        value, _ = self._select("3")
        self.assertEqual(value, "gamma")

    def test_empty_takes_the_default(self) -> None:
        value, _ = self._select("")
        self.assertEqual(value, "beta")

    def test_default_is_marked_with_an_asterisk(self) -> None:
        _, output = self._select("1")
        self.assertRegex(output, r"beta\s*\*")

    def test_typing_text_filters_to_a_unique_match(self) -> None:
        value, _ = self._select("gam")
        self.assertEqual(value, "gamma")

    def test_out_of_range_reprompts(self) -> None:
        value, output = self._select(["9", "1"])
        self.assertEqual(value, "alpha")
        self.assertIn("between 1 and 3", output)

    def test_no_match_reprompts(self) -> None:
        value, output = self._select(["zzz", "1"])
        self.assertEqual(value, "alpha")
        self.assertIn("Nothing matches", output)


if __name__ == "__main__":
    unittest.main()
