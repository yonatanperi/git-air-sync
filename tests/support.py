"""Shared test helpers: a headless reporter and throwaway git repositories."""

from __future__ import annotations

import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence


class ScriptedReporter:
    """A :class:`SyncReporter` that answers from a script instead of a human.

    Because it satisfies the same protocol as the real console reporter, the
    orchestration code under test is byte-for-byte the code that ships.
    """

    def __init__(
        self,
        *,
        confirm: bool | list[bool] = True,
        options: dict[str, str] | None = None,
        commit_choice: str | None = None,
    ) -> None:
        self._confirm = confirm
        self._options = options or {}
        self._commit_choice = commit_choice
        self.steps: list[str] = []
        self.warnings: list[tuple[str, list[str]]] = []
        self.infos: list[str] = []
        self.questions: list[str] = []

    @contextmanager
    def step(self, index: int, total: int, text: str) -> Iterator[None]:
        self.steps.append(f"[{index}/{total}] {text}")
        yield

    def info(self, text: str) -> None:
        self.infos.append(text)

    def warn(self, title: str, lines: list[str]) -> None:
        self.warnings.append((title, list(lines)))

    def confirm(self, question: str, *, default: bool = True) -> bool:
        self.questions.append(question)
        if isinstance(self._confirm, list):
            return self._confirm.pop(0) if self._confirm else default
        return self._confirm

    def choose_option(
        self, message: str, options: list[tuple[str, str]], default: str
    ) -> str:
        self.questions.append(message)
        return self._options.get("choice", default)

    def choose_commit(self, commits: Sequence[Any], message: str) -> str:
        self.questions.append(message)
        if self._commit_choice:
            return self._commit_choice
        return commits[-1].sha

    def show_commits(self, commits: Sequence[Any], title: str) -> None:
        self.infos.append(f"{title}: {len(commits)}")

    def show_files(self, files: Sequence[Any], title: str) -> None:
        self.infos.append(f"{title}: {len(files)}")


# ------------------------------------------------------------------------- git


GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "author@example.invalid",
    "GIT_COMMITTER_NAME": "Test Author",
    "GIT_COMMITTER_EMAIL": "author@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


def run(args: list[str], cwd: Path | None = None) -> str:
    env = {**os.environ, **GIT_ENV, "LC_ALL": "C"}
    proc = subprocess.run(
        args, cwd=str(cwd) if cwd else None, capture_output=True, text=True, env=env
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"{' '.join(args)} failed ({proc.returncode}):\n{proc.stderr}"
        )
    return proc.stdout


def init_repo(path: Path, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "-q", "-b", branch, str(path)])
    run(["git", "config", "user.name", "Test Author"], path)
    run(["git", "config", "user.email", "author@example.invalid"], path)
    return path


def commit(repo: Path, message: str, filename: str | None = None, body: str = "") -> str:
    if filename:
        target = repo / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body or message + "\n", encoding="utf-8")
        run(["git", "add", filename], repo)
        run(["git", "commit", "-q", "-m", message], repo)
    else:
        run(["git", "commit", "-q", "--allow-empty", "-m", message], repo)
    return head(repo)


def head(repo: Path) -> str:
    return run(["git", "rev-parse", "HEAD"], repo).strip()


def log_subjects(repo: Path) -> list[str]:
    out = run(["git", "log", "--format=%s"], repo).strip()
    return out.splitlines() if out else []
