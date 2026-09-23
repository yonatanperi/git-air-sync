"""Export and import orchestration.

Nothing here imports click, rich, or questionary. All interaction goes through the
:class:`SyncReporter` protocol, which means the exact same code path runs under the
real CLI and under the headless test-suite.
"""

from __future__ import annotations

import atexit
import hashlib
import re
import shutil
import socket
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Protocol

from .. import __version__
from ..config import Config, ProjectState, resolve_path, save
from ..errors import (
    EnvironmentError_,
    MergeConflict,
    NothingToDo,
    UserAbort,
)
from . import codec, envelope as env, git_ops as git, patterns as pat

SCRATCH_PREFIX = "git-air-sync-"
STALE_AFTER_SECONDS = 24 * 60 * 60


# --------------------------------------------------------------------- scratch dirs

_ACTIVE: set[Path] = set()


@contextmanager
def scratch_dir(prefix: str = SCRATCH_PREFIX) -> Iterator[Path]:
    """A temp directory removed on success, on exception, and on Ctrl-C.

    Patch files and dry-run worktrees never live inside the repository's working
    tree — they would show up as untracked files, could be committed by accident,
    and could be swept by ``git clean``.
    """
    directory = Path(tempfile.mkdtemp(prefix=prefix))
    _ACTIVE.add(directory)
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)
        _ACTIVE.discard(directory)


def cleanup_all() -> None:
    """Last-ditch sweep, registered with :mod:`atexit` by ``main``."""
    for directory in list(_ACTIVE):
        shutil.rmtree(directory, ignore_errors=True)
        _ACTIVE.discard(directory)


atexit.register(cleanup_all)


def sweep_stale_scratch() -> int:
    """Remove scratch dirs left by a previous run that was SIGKILLed."""
    removed = 0
    root = Path(tempfile.gettempdir())
    cutoff = time.time() - STALE_AFTER_SECONDS
    try:
        candidates = list(root.glob(SCRATCH_PREFIX + "*"))
    except OSError:
        return 0
    for candidate in candidates:
        try:
            if candidate.is_dir() and candidate.stat().st_mtime < cutoff:
                shutil.rmtree(candidate, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


# ------------------------------------------------------------------------ reporter


class SyncReporter(Protocol):
    """Everything the orchestration needs from a user interface."""

    def step(self, index: int, total: int, text: str) -> Any: ...
    def info(self, text: str) -> None: ...
    def warn(self, title: str, lines: list[str]) -> None: ...
    def confirm(self, question: str, *, default: bool = True) -> bool: ...
    def choose_option(
        self, message: str, options: list[tuple[str, str]], default: str
    ) -> str: ...
    def choose_commit(self, commits: list[git.CommitInfo], message: str) -> str: ...
    def show_commits(self, commits: list[git.CommitInfo], title: str) -> None: ...
    def show_files(self, files: list[git.FileChange], title: str) -> None: ...


# -------------------------------------------------------------------------- export


@dataclass
class ExportPlan:
    branch: str
    mode: str  # "full" | "incremental"
    base: str | None
    head: str
    revs: list[str]
    commit_count: int
    commits: list[git.CommitInfo]


@dataclass
class ExportResult:
    project: str
    path: Path
    plan: ExportPlan
    patch_bytes: int
    docx_bytes: int
    payload_sha256: str
    # Commits actually present in the transmitted patch series — can be lower
    # than plan.commit_count when exclude patterns dropped whole commits.
    commit_count: int = 0

    @property
    def ratio(self) -> float:
        return self.docx_bytes / self.patch_bytes if self.patch_bytes else 0.0


def resolve_export_plan(
    repo: Path,
    state: ProjectState,
    reporter: SyncReporter,
    *,
    force_full: bool = False,
    base_override: str | None = None,
    export_refs: str = "branch",
    assume_yes: bool = False,
) -> ExportPlan:
    """Work out what to export, asking the user when history has moved under us.

    Under ``assume_yes`` every question resolves to its documented default instead of
    prompting, so a scripted export never dead-ends on a non-interactive terminal.
    """

    def decide(message: str, options: list[tuple[str, str]], default: str) -> str:
        if assume_yes:
            return default
        return reporter.choose_option(message, options, default)

    if not git.has_commits(repo):
        raise EnvironmentError_(f"{repo} has no commits yet — nothing to export.")

    branch = git.current_branch(repo)
    if branch is None:
        raise EnvironmentError_(
            f"{repo} has a detached HEAD. Check out a branch before exporting, so the "
            "package records a real branch name for Computer B to apply."
        )

    head = git.resolve_sha(repo, branch)

    # Precedence: --full wins, then an explicit --base, then the recorded position.
    first_sync = state.last_synced_commit is None and base_override is None
    base = None if force_full else (base_override or state.last_synced_commit)

    if force_full or base_override:
        pass  # an explicit instruction; no reconciliation needed
    elif base and not git.rev_exists(repo, base):
        # Amend, rebase, or GC removed it — or the config points at the wrong repo.
        choice = decide(
            f"The last synced commit {base[:7]} no longer exists in this repository. "
            "It was probably rebased, amended, or garbage-collected.",
            [
                ("full", "Export the full history (produces a large document)"),
                ("pick", "Pick a new starting commit"),
                ("abort", "Abort"),
            ],
            default="full" if assume_yes else "pick",
        )
        if choice == "abort":
            raise UserAbort("Aborted at base-commit resolution.")
        base = None if choice == "full" else reporter.choose_commit(
            git.list_commits(repo, branch, limit=50),
            "Export commits after which commit?",
        )
    elif base and base == head:
        raise NothingToDo(f"'{branch}' is already synced at {head[:7]}.")
    elif base and not git.is_ancestor(repo, base, head):
        merge_point = git.merge_base(repo, base, head)
        options = [("full", "Export the full history")]
        if merge_point:
            options.insert(
                0,
                ("common", f"Export from the common ancestor {merge_point[:7]}"),
            )
        options.append(("abort", "Abort"))
        choice = decide(
            f"History has diverged: {base[:7]} is not an ancestor of {head[:7]}. "
            "Bundling this range anyway would produce a mess on Computer B.",
            options,
            default="common" if merge_point else "full",
        )
        if choice == "abort":
            raise UserAbort("Aborted at base-commit resolution.")
        base = merge_point if choice == "common" else None

    if first_sync and not force_full:
        # Never synced from this machine — let the user narrow it down.
        choice = decide(
            f"'{state.path or repo.name}' has never been synced from this machine.",
            [
                ("full", "Export the full history"),
                ("pick", "Start from a specific commit"),
                ("abort", "Abort"),
            ],
            default="full",
        )
        if choice == "abort":
            raise UserAbort("Aborted at base-commit resolution.")
        if choice == "pick":
            base = reporter.choose_commit(
                git.list_commits(repo, branch, limit=50),
                "Export commits after which commit?",
            )

    if base:
        rev_range = f"{base}..{branch}"
        count = git.count_commits(repo, rev_range)
        if count == 0:
            raise NothingToDo(f"'{branch}' is already synced at {head[:7]}.")
        revs = (
            ["--branches", "--tags", "--not", base]
            if export_refs == "all"
            else [rev_range]
        )
        commits = git.list_commits(repo, rev_range)
        return ExportPlan(branch, "incremental", base, head, revs, count, commits)

    count = git.count_commits(repo, branch)
    # `git format-patch <ref>` alone is shorthand for `<ref>..HEAD` (empty when they're
    # equal), unlike `git rev-list`/`git bundle create`'s "everything reachable from
    # ref" reading of a single positional argument — `--root` forces the full-history
    # reading `format-patch` actually needs here.
    revs = ["--root", "--branches", "--tags"] if export_refs == "all" else ["--root", branch]
    return ExportPlan(
        branch, "full", None, head, revs, count, git.list_commits(repo, branch)
    )


def export_project(
    repo: Path,
    project: str,
    state: ProjectState,
    out_dir: Path,
    reporter: SyncReporter,
    *,
    force_full: bool = False,
    base_override: str | None = None,
    assume_yes: bool = False,
    max_payload_mb: int = 25,
    export_refs: str = "branch",
    exclude_patterns: list[str] | None = None,
) -> ExportResult:
    with reporter.step(1, 4, "Scanning repository"):
        plan = resolve_export_plan(
            repo,
            state,
            reporter,
            force_full=force_full,
            base_override=base_override,
            export_refs=export_refs,
            assume_yes=assume_yes,
        )
        tree = git.working_tree_status(repo)

    if tree.dirty and not assume_yes:
        reporter.warn(
            "Uncommitted changes",
            [
                f"This repository has uncommitted work ({tree.summary()}).",
                "",
                "A patch package carries committed changes only. These changes will NOT",
                "be included in the export and will NOT reach Computer B. Commit them",
                "first if you want them to travel.",
            ],
        )
        if not reporter.confirm("Export anyway?", default=False):
            raise UserAbort("Aborted because of uncommitted changes.")

    if not assume_yes:
        reporter.show_commits(
            plan.commits,
            f"{plan.commit_count} commit(s) to export from '{plan.branch}'",
        )
        if not reporter.confirm(f"Export these {plan.commit_count} commit(s)?", default=True):
            raise UserAbort("Aborted at the commit preview.")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with scratch_dir() as scratch:
        patch_path = scratch / f"{project}.patch"

        with reporter.step(2, 4, "Creating patch series"):
            git.format_patch(repo, patch_path, plan.revs, exclude_patterns=exclude_patterns)
            patch_bytes = patch_path.read_bytes()

        if not patch_bytes:
            raise EnvironmentError_("git produced an empty patch series.")

        # Excluding files can drop whole commits from the series (a commit whose
        # entire diff falls under an excluded pattern is left out entirely), so
        # the transmitted count can be lower than `plan.commit_count`, which was
        # computed before any exclusion was applied.
        actual_commit_count = len(re.findall(rb"(?m)^From [0-9a-fA-F]{40,64} ", patch_bytes))

        estimated = codec.estimate_docx_size(len(patch_bytes))
        _check_disk_space(out_dir, estimated)

        if estimated > max_payload_mb * 1024 * 1024 and not assume_yes:
            reporter.warn(
                "Large document",
                [
                    f"The patch series is {_human(len(patch_bytes))} and the .docx will be",
                    f"around {_human(estimated)} — over the {max_payload_mb} MB limit",
                    "configured for this machine.",
                    "",
                    "Many transfer channels cap attachment size. Consider exporting from",
                    "a more recent base commit instead.",
                ],
            )
            if not reporter.confirm("Continue anyway?", default=False):
                raise UserAbort("Aborted because of payload size.")

        with reporter.step(3, 4, "Encoding payload"):
            meta = env.Envelope(
                project=project,
                source_branch=plan.branch,
                package_mode=plan.mode,
                base_sha=plan.base,
                head_sha=plan.head,
                commit_count=actual_commit_count,
                hash_algo=git.object_format(repo),
                created_at=_utc_now(),
                created_by=f"{socket.gethostname()}",
                payload_size=len(patch_bytes),
                payload_sha256="",  # recomputed by wrap()
                tool_version=__version__,
            )
            blob = env.wrap(patch_bytes, meta)
            out_path = out_dir / env.suggested_filename(meta)

        with reporter.step(4, 4, "Writing document"):
            docx_bytes = codec.encode_bytes_to_docx(blob, out_path)

    return ExportResult(
        project=project,
        path=out_path,
        plan=plan,
        patch_bytes=len(patch_bytes),
        docx_bytes=docx_bytes,
        payload_sha256=hashlib.sha256(patch_bytes).hexdigest(),
        commit_count=actual_commit_count,
    )


# -------------------------------------------------------------------------- import


@dataclass
class ImportResult:
    project: str
    repo: Path
    envelope: env.Envelope
    outcome: git.ApplyOutcome
    commits: list[git.CommitInfo]
    files: list[git.FileChange]
    conflicts: list[str]
    bootstrapped: bool = False
    applied: bool = False
    # This machine's own branch HEAD after applying — NOT `envelope.head_sha`
    # (Computer A's hash), which `git am` never reproduces. None when nothing was
    # actually applied to the real repo (the --no-merge / do_merge=False path).
    local_head: str | None = None
    auto_resolved: list[str] = field(default_factory=list)


@dataclass
class AmOutcome:
    ok: bool  # applied clean, or every conflict was auto-resolved
    conflicts: list[str]  # genuinely unresolved conflicts (empty when ok)
    auto_resolved: list[str]  # paths kept-local because they matched an exclude pattern


def _apply_with_auto_resolve(
    repo: Path, patch_path: Path, exclude_patterns: list[str]
) -> AmOutcome:
    """Runs ``git am --3way``, then hands off to :func:`_drain_conflicts`."""
    result = git.am_apply(repo, patch_path, three_way=True)
    return _drain_conflicts(repo, exclude_patterns, result)


def _drain_conflicts(repo: Path, exclude_patterns: list[str], result: git.GitResult) -> AmOutcome:
    """While ``am`` is stopped on conflicts, auto-resolve any conflicted path
    that matches ``exclude_patterns`` by keeping the local copy, and continue —
    looping, since a patch series has multiple commits and resolving one can
    reveal a conflict in the next. Stops the instant a genuine (non-excluded)
    conflict shows up, leaving ``am`` mid-conflict exactly as before this
    feature existed, so the normal abort/resolve flow is untouched for real
    conflicts. ``result`` is the outcome of whatever just ran (``am``,
    ``am --continue``, or ``am --skip``) immediately before this call.
    """
    auto_resolved: list[str] = []
    while not result.ok and git.am_in_progress(repo):
        conflicts = git.conflicted_files(repo)
        excluded = [p for p in conflicts if pat.matches_any(p, exclude_patterns)]
        genuine = [p for p in conflicts if p not in excluded]
        if genuine:
            return AmOutcome(ok=False, conflicts=genuine, auto_resolved=auto_resolved)
        for path in excluded:
            git.resolve_conflict_keep_ours(repo, path)
        auto_resolved.extend(excluded)
        result = git.continue_or_skip_am(repo)

    if not result.ok and not git.am_in_progress(repo):
        # A hard failure unrelated to conflicts (corrupt patch, hook failure, …) —
        # surface it exactly like before, don't swallow it as "conflicts".
        detail = (result.stderr or result.stdout).strip()
        raise EnvironmentError_(f"Applying the patch series failed:\n{detail}")

    return AmOutcome(ok=True, conflicts=[], auto_resolved=auto_resolved)


def import_document(
    docx: Path,
    cfg: Config,
    reporter: SyncReporter,
    *,
    repo_override: Path | None = None,
    do_merge: bool = True,
    assume_yes: bool = False,
) -> ImportResult:
    docx = Path(docx)

    with reporter.step(1, 4, "Reading document"):
        blob = codec.decode_docx_to_bytes(docx)
        meta, patch_bytes = env.unwrap(blob)

    with reporter.step(2, 4, "Locating project"):
        repo = repo_override or _locate_repo(meta, cfg)

    state = cfg.project(meta.project)
    if state.last_payload_sha256 and meta.payload_sha256 == state.last_payload_sha256:
        raise NothingToDo(f"'{meta.project}' has already imported this exact package.")

    effective_excludes = cfg.effective_exclude_patterns(meta.project)

    with scratch_dir() as scratch:
        patch_path = scratch / f"{meta.project}.patch"
        patch_path.write_bytes(patch_bytes)

        # Bootstrap: the project doesn't exist here yet.
        if repo is None:
            if meta.package_mode != "full":
                raise EnvironmentError_(
                    f"'{meta.project}' does not exist on this machine, and this package "
                    f"is an incremental update starting at {meta.short(meta.base_sha)}. "
                    "Ask Computer A to export the full history:\n"
                    f"    git-air-sync export {meta.project} --full"
                )
            root = resolve_path(cfg.projects_root)
            if root is None:
                raise EnvironmentError_(
                    "No projects root is configured. Run 'git-air-sync config' first."
                )
            dest = root / meta.project
            if not assume_yes and not reporter.confirm(
                f"'{meta.project}' is not on this machine. Create it at {dest}?",
                default=True,
            ):
                raise UserAbort("Aborted before bootstrap.")
            with reporter.step(3, 4, "Creating repository"):
                git.init_repo(dest, meta.source_branch)
                result = git.am_apply(dest, patch_path, three_way=True)
                if not result.ok:
                    detail = (result.stderr or result.stdout).strip()
                    raise EnvironmentError_(
                        "Applying the full history to a brand-new repository failed "
                        f"unexpectedly:\n{detail}"
                    )
            commits = git.list_commits(dest, meta.source_branch, limit=50)
            import_head = git.resolve_sha(dest, meta.source_branch)
            _record_import(cfg, meta, dest, import_head)
            return ImportResult(
                project=meta.project,
                repo=dest,
                envelope=meta,
                outcome=git.ApplyOutcome.APPLIED,
                commits=commits,
                files=[],
                conflicts=[],
                bootstrapped=True,
                applied=True,
                local_head=import_head,
            )

        ours = git.current_branch(repo) or "HEAD"
        original_tip = git.resolve_sha(repo, ours)

        with reporter.step(3, 4, "Checking for conflicts"):
            with scratch_dir(prefix="git-air-sync-preview-") as preview_scratch:
                worktree = preview_scratch / "wt"
                git.add_worktree(repo, worktree, ours)
                try:
                    dry_run = _apply_with_auto_resolve(worktree, patch_path, effective_excludes)
                    clean = dry_run.ok
                    conflicts = dry_run.conflicts
                    dry_auto_resolved = dry_run.auto_resolved
                    commits = git.list_commits(worktree, f"{original_tip}..HEAD")
                    files = git.changed_files(worktree, original_tip, "HEAD")
                    if not clean and git.am_in_progress(worktree):
                        git.abort_am(worktree)
                finally:
                    git.remove_worktree(repo, worktree)

        if not do_merge:
            reporter.show_commits(commits, f"{len(commits)} incoming commit(s)")
            manual_patch = docx.with_suffix(".patch")
            shutil.copyfile(patch_path, manual_patch)
            reporter.info(
                f"Patch series saved to {manual_patch}. Apply it yourself with:\n"
                f"    git -C {repo} am --3way {manual_patch}"
            )
            return ImportResult(
                meta.project, repo, meta,
                git.ApplyOutcome.APPLIED if clean else git.ApplyOutcome.CONFLICT,
                commits, files, conflicts, applied=False, auto_resolved=dry_auto_resolved,
            )

        if not assume_yes:
            reporter.show_commits(commits, f"{len(commits)} incoming commit(s)")
            reporter.show_files(files, "Files affected")
            if clean:
                note = "This will apply cleanly."
                if dry_auto_resolved:
                    note += (
                        f" ({len(dry_auto_resolved)} excluded file(s) will be "
                        "auto-resolved by keeping your local version.)"
                    )
                reporter.info(note)
            else:
                reporter.warn(
                    "Conflicts predicted",
                    [
                        "Applying will produce conflicts in:",
                        *(f"  {p}" for p in conflicts[:20]),
                        "",
                        "You will be able to resolve them with normal git tools.",
                    ],
                )
            if not reporter.confirm(f"Apply into '{ours}'?", default=True):
                raise UserAbort("Aborted at the conflict preview.")

        tree = git.working_tree_status(repo)
        if tree.dirty:
            reporter.warn(
                "Uncommitted changes",
                [
                    f"'{repo.name}' has uncommitted work ({tree.summary()}).",
                    "",
                    "Applying on top of a dirty working tree can leave you with a mess",
                    "that is hard to unpick. Commit or stash first.",
                ],
            )
            if not assume_yes and not reporter.confirm("Apply anyway?", default=False):
                raise UserAbort("Aborted because of uncommitted changes.")

        with reporter.step(4, 4, "Applying patches"):
            # Raised inside the step so it reports failure rather than printing
            # "done" and then contradicting itself with a conflict panel.
            outcome = _apply_with_auto_resolve(repo, patch_path, effective_excludes)
            if not outcome.ok:
                _record_conflict(cfg, meta, repo, outcome.conflicts, outcome.auto_resolved)
                raise MergeConflictDetail(
                    meta, repo, outcome.conflicts, commits, files, outcome.auto_resolved
                )

    import_head = git.resolve_sha(repo, ours)
    _record_import(cfg, meta, repo, import_head)

    return ImportResult(
        meta.project, repo, meta, git.ApplyOutcome.APPLIED, commits, files, [],
        applied=True, local_head=import_head, auto_resolved=outcome.auto_resolved,
    )


class MergeConflictDetail(MergeConflict):
    """A conflict, carrying everything the UI needs to render the alert panel."""

    def __init__(
        self,
        meta: env.Envelope,
        repo: Path,
        conflicts: list[str],
        commits: list[git.CommitInfo],
        files: list[git.FileChange],
        auto_resolved: list[str] | None = None,
    ) -> None:
        super().__init__(f"{len(conflicts)} conflicted file(s) in {repo}")
        self.envelope = meta
        self.repo = repo
        self.conflicts = conflicts
        self.commits = commits
        self.files = files
        self.auto_resolved = auto_resolved or []


# ------------------------------------------------------------------------- resolve


def finalize_resolution(repo: Path, project: str, cfg: Config) -> bool:
    """Finish an import that stopped with conflicts. Returns True when complete."""
    remaining = git.conflicted_files(repo)
    if remaining:
        raise MergeConflict(
            f"{len(remaining)} file(s) still have unresolved conflicts:\n"
            + "\n".join(f"  {p}" for p in remaining)
            + "\n\nResolve them, 'git add' each one, then run 'git-air-sync resolve' again."
        )

    if git.am_in_progress(repo):
        result = git.continue_or_skip_am(repo)
        # Resolving that commit can uncover a conflict further along the patch
        # series — auto-resolve it too if it's on an excluded path, exactly like
        # the initial apply, so an irrelevant file never needs a second manual
        # round-trip through 'resolve'.
        outcome = _drain_conflicts(repo, cfg.effective_exclude_patterns(project), result)
        if not outcome.ok:
            raise MergeConflict(
                f"Resolving that file uncovered {len(outcome.conflicts)} more "
                "conflicted file(s) further along the patch series:\n"
                + "\n".join(f"  {p}" for p in outcome.conflicts)
                + "\n\nResolve them, 'git add' each one, then run 'git-air-sync resolve' again."
            )

    branch = git.current_branch(repo) or "HEAD"
    state = cfg.project(project)
    pending = state.pending_conflict or {}
    head = pending.get("head_sha")
    if head:
        state.last_synced_commit = head
        state.last_synced_branch = pending.get("source_branch")
        state.last_sync_at = _utc_now()
        state.last_payload_sha256 = pending.get("payload_sha256")
        state.last_import_head = git.resolve_sha(repo, branch)
    state.pending_conflict = None
    save(cfg)

    return True


# ------------------------------------------------------------------------- helpers


def _locate_repo(meta: env.Envelope, cfg: Config) -> Path | None:
    direct = cfg.project_path(meta.project)
    if direct and direct.exists() and git.is_git_repo(direct):
        return direct

    root = resolve_path(cfg.projects_root)
    if root and root.exists():
        for candidate in git.discover_repos(root):
            if candidate.name == meta.project:
                return candidate
    return None


def _record_import(cfg: Config, meta: env.Envelope, repo: Path, import_head: str) -> None:
    state = cfg.project(meta.project)
    state.path = str(repo)
    state.last_synced_commit = meta.head_sha
    state.last_synced_branch = meta.source_branch
    state.last_sync_at = _utc_now()
    state.last_payload_sha256 = meta.payload_sha256
    state.last_import_head = import_head
    state.pending_conflict = None
    save(cfg)


def _record_conflict(
    cfg: Config,
    meta: env.Envelope,
    repo: Path,
    conflicts: list[str],
    auto_resolved: list[str] | None = None,
) -> None:
    state = cfg.project(meta.project)
    state.path = str(repo)
    # last_synced_commit is deliberately NOT advanced: the merge isn't done.
    state.pending_conflict = {
        "head_sha": meta.head_sha,
        "source_branch": meta.source_branch,
        "payload_sha256": meta.payload_sha256,
        "files": conflicts,
        "auto_resolved": auto_resolved or [],
        "detected_at": _utc_now(),
    }
    save(cfg)


def _check_disk_space(out_dir: Path, estimated: int) -> None:
    try:
        free = shutil.disk_usage(out_dir).free
    except OSError:
        return
    needed = estimated * 3  # patch series + in-memory document + the .part file
    if free < needed:
        raise EnvironmentError_(
            f"Not enough free space in {out_dir}: about {_human(needed)} is needed but "
            f"only {_human(free)} is available."
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


human_size = _human
