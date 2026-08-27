"""Native git, driven through subprocess.

Every command goes through :func:`run_git`, which pins the locale so the handful of
messages we parse stay in English, disables credential prompting so nothing can hang,
and never uses ``shell=True``.

Structured reads use ``-z`` or explicit record separators rather than line splitting,
so commit subjects and paths containing newlines, quotes, or unicode are safe.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..errors import EnvironmentError_

# Record/unit separators — cannot occur in a commit subject.
_RS = "\x1e"
_US = "\x1f"

INCOMING_REF = "refs/air-sync/incoming"


class GitMissingError(EnvironmentError_):
    """git is not on PATH."""


@dataclass(frozen=True)
class GitResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class GitError(RuntimeError):
    def __init__(self, result: GitResult) -> None:
        detail = (result.stderr or result.stdout).strip() or "(no output)"
        super().__init__(f"git {' '.join(result.args)} failed:\n{detail}")
        self.result = result


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    short: str
    author: str
    date: str
    subject: str


@dataclass(frozen=True)
class FileChange:
    status: str  # A, M, D, R, C, T
    path: str
    old_path: str | None = None

    @property
    def label(self) -> str:
        if self.old_path:
            return f"{self.old_path} -> {self.path}"
        return self.path


@dataclass(frozen=True)
class WorkingTree:
    branch: str | None
    detached: bool
    staged: int
    unstaged: int
    untracked: int

    @property
    def dirty(self) -> bool:
        return bool(self.staged or self.unstaged or self.untracked)

    def summary(self) -> str:
        bits = []
        if self.staged:
            bits.append(f"{self.staged} staged")
        if self.unstaged:
            bits.append(f"{self.unstaged} modified")
        if self.untracked:
            bits.append(f"{self.untracked} untracked")
        return ", ".join(bits) or "clean"


@dataclass(frozen=True)
class BundleVerification:
    ok: bool
    complete_history: bool
    refs: dict[str, str]
    missing_prereqs: list[str]
    not_a_bundle: bool
    raw: str


class MergeOutcome(Enum):
    ALREADY_UP_TO_DATE = "already-up-to-date"
    FAST_FORWARD = "fast-forward"
    MERGED = "merged"
    CONFLICT = "conflict"
    FAILED = "failed"


@dataclass(frozen=True)
class MergePreview:
    clean: bool
    conflicts: list[str]
    unrelated_histories: bool
    raw: str


# --------------------------------------------------------------------------- runner


def git_executable() -> str:
    exe = shutil.which("git")
    if not exe:
        raise GitMissingError(
            "git was not found on your PATH. git-air-sync is a thin layer over native "
            "git and cannot do anything without it."
        )
    return exe


def _env() -> dict[str, str]:
    env = dict(os.environ)
    # Stable English for the few messages we parse.
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    # Never block waiting for credentials; there is no network on Computer B anyway.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    # A stray GIT_DIR in the caller's environment would silently retarget every command.
    for leaked in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(leaked, None)
    return env


def run_git(
    args: list[str],
    *,
    cwd: Path | str | None = None,
    check: bool = True,
    timeout: int = 600,
) -> GitResult:
    argv = [git_executable(), *args]
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            env=_env(),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(
            GitResult(tuple(args), 124, "", f"timed out after {timeout}s")
        ) from exc

    result = GitResult(tuple(args), proc.returncode, proc.stdout, proc.stderr)
    if check and not result.ok:
        raise GitError(result)
    return result


def _repo(repo: Path | str, args: list[str]) -> list[str]:
    return ["-C", str(repo), *args]


# ----------------------------------------------------------------------- inspection


def git_version() -> str:
    return run_git(["--version"]).stdout.strip()


def is_git_repo(path: Path) -> bool:
    return run_git(_repo(path, ["rev-parse", "--git-dir"]), check=False).ok


def repo_root(path: Path) -> Path:
    return Path(run_git(_repo(path, ["rev-parse", "--show-toplevel"])).stdout.strip())


def discover_repos(root: Path, depth: int = 2) -> list[Path]:
    """Find git repos under ``root``, pruning once one is found."""
    found: list[Path] = []

    def walk(directory: Path, level: int) -> None:
        if level > depth:
            return
        try:
            entries = sorted(p for p in directory.iterdir() if p.is_dir())
        except OSError:
            return
        for entry in entries:
            if entry.name.startswith(".") and entry.name != ".":
                continue
            if (entry / ".git").exists():
                found.append(entry)
                continue  # don't descend into a repo
            walk(entry, level + 1)

    walk(Path(root), 1)
    return found


def has_commits(repo: Path) -> bool:
    return run_git(_repo(repo, ["rev-parse", "--verify", "--quiet", "HEAD"]), check=False).ok


def current_branch(repo: Path) -> str | None:
    """Branch name, or ``None`` when HEAD is detached."""
    result = run_git(_repo(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"]), check=False)
    return result.stdout.strip() if result.ok else None


def resolve_sha(repo: Path, rev: str) -> str:
    return run_git(
        _repo(repo, ["rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}"])
    ).stdout.strip()


def rev_exists(repo: Path, rev: str) -> bool:
    return run_git(_repo(repo, ["cat-file", "-e", f"{rev}^{{commit}}"]), check=False).ok


def object_format(repo: Path) -> str:
    result = run_git(_repo(repo, ["rev-parse", "--show-object-format"]), check=False)
    return result.stdout.strip() if result.ok else "sha1"


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    result = run_git(
        _repo(repo, ["merge-base", "--is-ancestor", ancestor, descendant]), check=False
    )
    if result.returncode in (0, 1):
        return result.returncode == 0
    raise GitError(result)


def merge_base(repo: Path, a: str, b: str) -> str | None:
    result = run_git(_repo(repo, ["merge-base", a, b]), check=False)
    return result.stdout.strip() if result.ok else None


def count_commits(repo: Path, rev_range: str) -> int:
    result = run_git(_repo(repo, ["rev-list", "--count", rev_range]), check=False)
    return int(result.stdout.strip() or 0) if result.ok else 0


def list_commits(repo: Path, rev_range: str, limit: int | None = None) -> list[CommitInfo]:
    fmt = _US.join(["%H", "%h", "%an", "%ad", "%s"]) + _RS
    args = ["log", "--no-color", "--date=iso-strict", f"--pretty=format:{fmt}"]
    if limit:
        args += ["-n", str(limit)]
    args.append(rev_range)

    result = run_git(_repo(repo, args), check=False)
    if not result.ok:
        return []

    commits = []
    for record in result.stdout.split(_RS):
        record = record.strip("\n")
        if not record:
            continue
        # Bounded split: the subject is last, so a separator byte inside it (rare,
        # but git preserves whatever the author wrote) must not shift the fields.
        parts = record.split(_US, 4)
        if len(parts) < 5:
            continue
        sha, short, author, date, subject = parts
        commits.append(CommitInfo(sha, short, author, date, subject))
    return commits


def changed_files(repo: Path, base: str, head: str) -> list[FileChange]:
    """``git diff --name-status -z``, handling rename/copy's two-path records."""
    result = run_git(
        _repo(repo, ["diff", "--name-status", "-z", base, head]), check=False
    )
    if not result.ok:
        return []

    fields = [f for f in result.stdout.split("\0") if f != ""]
    changes: list[FileChange] = []
    i = 0
    while i < len(fields):
        status = fields[i]
        i += 1
        if not status:
            continue
        if status[0] in ("R", "C"):
            if i + 1 >= len(fields):
                break
            old_path, new_path = fields[i], fields[i + 1]
            i += 2
            changes.append(FileChange(status[0], new_path, old_path))
        else:
            if i >= len(fields):
                break
            changes.append(FileChange(status[0], fields[i]))
            i += 1
    return changes


def working_tree_status(repo: Path) -> WorkingTree:
    result = run_git(
        _repo(repo, ["status", "--porcelain=v2", "--branch", "-z"]), check=False
    )
    if not result.ok:
        return WorkingTree(None, False, 0, 0, 0)

    branch: str | None = None
    detached = False
    staged = unstaged = untracked = 0

    for entry in result.stdout.split("\0"):
        if not entry:
            continue
        if entry.startswith("# branch.head "):
            name = entry[len("# branch.head ") :].strip()
            if name == "(detached)":
                detached = True
            else:
                branch = name
        elif entry.startswith("?"):
            untracked += 1
        elif entry[0] in ("1", "2"):
            # "1 <XY> ..." — X is the staged status, Y the unstaged one.
            parts = entry.split(" ")
            if len(parts) > 1 and len(parts[1]) >= 2:
                x, y = parts[1][0], parts[1][1]
                if x != ".":
                    staged += 1
                if y != ".":
                    unstaged += 1
        elif entry.startswith("u "):
            unstaged += 1

    return WorkingTree(branch, detached, staged, unstaged, untracked)


# --------------------------------------------------------------------------- bundles


def create_bundle(repo: Path, out_path: Path, revs: list[str]) -> None:
    """``git bundle create``.

    Callers must ensure the range is non-empty — git exits non-zero with
    "Refusing to create empty bundle." otherwise, which reads like a crash.
    """
    run_git(_repo(repo, ["bundle", "create", "--quiet", str(out_path), *revs]))


_PREREQ_HEADING = "prerequisite"
_SHA_RE = re.compile(r"\b([0-9a-f]{40}|[0-9a-f]{64})\b")


def verify_bundle(repo: Path, bundle_path: Path) -> BundleVerification:
    """``git bundle verify``, run *inside the destination repo*.

    Running it there is the whole point: that is what turns it into the check for
    "does this repo have the base commit this incremental bundle continues from".
    """
    result = run_git(
        _repo(repo, ["bundle", "verify", str(bundle_path)]), check=False
    )
    raw = (result.stderr or "") + (result.stdout or "")
    lowered = raw.lower()

    not_a_bundle = "does not look like a v2 or v3 bundle" in lowered

    missing: list[str] = []
    if not result.ok and _PREREQ_HEADING in lowered:
        collecting = False
        for line in raw.splitlines():
            stripped = line.strip()
            if _PREREQ_HEADING in stripped.lower():
                collecting = True
                continue
            if collecting:
                match = _SHA_RE.search(stripped)
                if match:
                    missing.append(match.group(1))
                elif stripped:
                    collecting = False
    if not result.ok and not missing and not not_a_bundle:
        # Heading wording varies across git versions; fall back to any SHA present.
        missing = _SHA_RE.findall(raw)

    return BundleVerification(
        ok=result.ok,
        complete_history="complete history" in lowered,
        refs=list_bundle_heads(repo, bundle_path),
        missing_prereqs=list(dict.fromkeys(missing)),
        not_a_bundle=not_a_bundle,
        raw=raw.strip(),
    )


def list_bundle_heads(repo: Path, bundle_path: Path) -> dict[str, str]:
    """Refs a bundle carries. Works even when prerequisites are missing."""
    result = run_git(
        _repo(repo, ["bundle", "list-heads", str(bundle_path)]), check=False
    )
    if not result.ok:
        return {}
    refs: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            refs[parts[1]] = parts[0]
    return refs


def fetch_from_bundle(repo: Path, bundle_path: Path, src_ref: str, dst_ref: str) -> None:
    """Fetch one ref out of a bundle. Git treats a bundle path as a remote."""
    run_git(
        _repo(
            repo,
            [
                "fetch",
                "--no-tags",
                "--no-write-fetch-head",
                str(bundle_path),
                f"+{src_ref}:{dst_ref}",
            ],
        )
    )


def clone_from_bundle(bundle_path: Path, dest: Path, branch: str | None = None) -> None:
    """Bootstrap a repo that doesn't exist yet on Computer B from a full bundle.

    ``origin`` is removed afterwards because it would otherwise point at a temporary
    bundle path that is deleted moments later.
    """
    args = ["clone"]
    if branch:
        args += ["--branch", branch]
    args += [str(bundle_path), str(dest)]
    run_git(args)
    run_git(_repo(dest, ["remote", "remove", "origin"]), check=False)


# ---------------------------------------------------------------------------- merge


def preview_merge(repo: Path, ours: str, theirs: str) -> MergePreview:
    """Predict the merge without touching the working tree (git >= 2.38)."""
    result = run_git(
        _repo(repo, ["merge-tree", "--write-tree", "--name-only", "--messages", ours, theirs]),
        check=False,
    )
    raw = (result.stdout or "") + (result.stderr or "")

    if result.returncode == 0:
        return MergePreview(True, [], False, raw.strip())

    if result.returncode == 1:
        # stdout is: <tree oid>\n<conflicted paths...>\n\n<messages>
        blocks = result.stdout.split("\n\n")
        lines = blocks[0].splitlines() if blocks else []
        conflicts = [ln.strip() for ln in lines[1:] if ln.strip()]
        return MergePreview(False, conflicts, False, raw.strip())

    return MergePreview(
        False, [], "unrelated histories" in raw.lower(), raw.strip()
    )


def merge_ref(
    repo: Path, ref: str, *, message: str, allow_ff: bool = True,
    allow_unrelated: bool = False,
) -> tuple[MergeOutcome, str]:
    args = ["merge", "--no-edit", "--ff" if allow_ff else "--no-ff", "-m", message]
    if allow_unrelated:
        args.append("--allow-unrelated-histories")
    args.append(ref)

    result = run_git(_repo(repo, args), check=False)
    output = (result.stdout or "") + (result.stderr or "")

    if result.ok:
        lowered = output.lower()
        if "already up to date" in lowered:
            return MergeOutcome.ALREADY_UP_TO_DATE, output.strip()
        if "fast-forward" in lowered:
            return MergeOutcome.FAST_FORWARD, output.strip()
        return MergeOutcome.MERGED, output.strip()

    if conflicted_files(repo):
        return MergeOutcome.CONFLICT, output.strip()
    return MergeOutcome.FAILED, output.strip()


def conflicted_files(repo: Path) -> list[str]:
    result = run_git(
        _repo(repo, ["diff", "--name-only", "--diff-filter=U", "-z"]), check=False
    )
    if not result.ok:
        return []
    return [p for p in result.stdout.split("\0") if p]


def merge_in_progress(repo: Path) -> bool:
    return run_git(
        _repo(repo, ["rev-parse", "--verify", "--quiet", "MERGE_HEAD"]), check=False
    ).ok


def abort_merge(repo: Path) -> bool:
    return run_git(_repo(repo, ["merge", "--abort"]), check=False).ok


def delete_ref(repo: Path, ref: str) -> None:
    run_git(_repo(repo, ["update-ref", "-d", ref]), check=False)


def ref_exists(repo: Path, ref: str) -> bool:
    return run_git(
        _repo(repo, ["rev-parse", "--verify", "--quiet", ref]), check=False
    ).ok
