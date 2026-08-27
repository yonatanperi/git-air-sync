"""The concrete :class:`~git_air_sync.core.sync.SyncReporter` used by the real CLI.

The test-suite substitutes a scripted implementation of the same protocol, so both
run the identical orchestration code.
"""

from __future__ import annotations

from typing import Any, Sequence

from . import displays, prompts
from .prompts import Choice
from .theme import Pill


class ConsoleReporter:
    def step(self, index: int, total: int, text: str) -> Any:
        return displays.step(index, total, text)

    def info(self, text: str) -> None:
        displays.note(text, Pill.INFO)

    def warn(self, title: str, lines: list[str]) -> None:
        displays.panel(title, lines, Pill.PENDING)

    def confirm(self, question: str, *, default: bool = True) -> bool:
        return prompts.confirm(question, default=default, flag="--yes")

    def choose_option(
        self, message: str, options: list[tuple[str, str]], default: str
    ) -> str:
        displays.panel("Decision needed", [message], Pill.PENDING)
        return prompts.select(
            "How would you like to proceed?",
            [
                Choice(value=value, label=label, is_default=value == default)
                for value, label in options
            ],
            default=default,
        )

    def choose_commit(self, commits: Sequence[Any], message: str) -> str:
        if not commits:
            raise prompts.UserAbort("This repository has no commits to choose from.")
        displays.commit_table(commits, "Recent commits")
        return prompts.select(
            message,
            [
                Choice(
                    value=c.sha,
                    label=f"{c.short}  {c.subject[:60]}",
                    description=c.date[:10],
                )
                for c in commits
            ],
        )

    def show_commits(self, commits: Sequence[Any], title: str) -> None:
        displays.commit_table(commits, title)

    def show_files(self, files: Sequence[Any], title: str) -> None:
        displays.file_table(files, title)
