"""Export and import orchestration.

Nothing here imports click, rich, or questionary. All interaction goes through the
:class:`SyncReporter` protocol, which means the exact same code path runs under the
real CLI and under the headless test-suite.
"""

from __future__ import annotations

import atexit
import hashlib
import shutil
import socket
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Protocol

from .. import __version__
from ..config import Config, ProjectState, resolve_path, save
from ..errors import (
    EnvironmentError_,
    MergeConflict,
    NothingToDo,
    PayloadError,
    UserAbort,
)
from . import codec, envelope as env, git_ops as git

SCRATCH_PREFIX = "git-air-sync-"
STALE_AFTER_SECONDS = 24 * 60 * 60


# --------------------------------------------------------------------- scratch dirs

_ACTIVE: set[Path] = set()


@contextmanager
def scratch_dir(prefix: str = SCRATCH_PREFIX) -> Iterator[Path]:
    """A temp directory removed on success, on exception, and on Ctrl-C.

    Bundles never touch the repository working tree — they would show up as untracked
    files, could be committed by accident, and could be swept by ``git clean``.
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
    bundle_bytes: int
    docx_bytes: int
    payload_sha256: str

    @property
    def ratio(self) -> float:
        return self.docx_bytes / self.bundle_bytes if self.bundle_bytes else 0.0


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
    """Work out what to bundle, asking the user when history has moved under us.

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
            "bundle records a real branch name for Computer B to merge."
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
    revs = ["--branches", "--tags"] if export_refs == "all" else [branch]
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
                "A git bundle carries commits only. These changes will NOT be included",
                "in the export and will NOT reach Computer B. Commit them first if you",
                "want them to travel.",
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
        bundle_path = scratch / f"{project}.bundle"

        with reporter.step(2, 4, "Creating bundle"):
            git.create_bundle(repo, bundle_path, plan.revs)
            bundle = bundle_path.read_bytes()

        if not bundle:
            raise EnvironmentError_("git produced an empty bundle.")

        estimated = codec.estimate_docx_size(len(bundle))
        _check_disk_space(out_dir, estimated)

        if estimated > max_payload_mb * 1024 * 1024 and not assume_yes:
            reporter.warn(
                "Large document",
                [
                    f"The bundle is {_human(len(bundle))} and the .docx will be around",
                    f"{_human(estimated)} — over the {max_payload_mb} MB limit configured",
                    "for this machine.",
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
                bundle_mode=plan.mode,
                base_sha=plan.base,
                head_sha=plan.head,
                commit_count=plan.commit_count,
                hash_algo=git.object_format(repo),
                created_at=_utc_now(),
                created_by=f"{socket.gethostname()}",
                payload_size=len(bundle),
                payload_sha256="",  # recomputed by wrap()
                tool_version=__version__,
            )
            blob = env.wrap(bundle, meta)
            out_path = out_dir / env.suggested_filename(meta)

        with reporter.step(4, 4, "Writing document"):
            docx_bytes = codec.encode_bytes_to_docx(blob, out_path)

    return ExportResult(
        project=project,
        path=out_path,
        plan=plan,
        bundle_bytes=len(bundle),
        docx_bytes=docx_bytes,
        payload_sha256=hashlib.sha256(bundle).hexdigest(),
    )


# -------------------------------------------------------------------------- import


@dataclass
class ImportResult:
    project: str
    repo: Path
    envelope: env.Envelope
    outcome: git.MergeOutcome
    commits: list[git.CommitInfo]
    files: list[git.FileChange]
    conflicts: list[str]
    bootstrapped: bool = False
    merged: bool = False


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

    with reporter.step(1, 5, "Reading document"):
        blob = codec.decode_docx_to_bytes(docx)
        meta, bundle = env.unwrap(blob)

    with reporter.step(2, 5, "Locating project"):
        repo = repo_override or _locate_repo(meta, cfg)

    with scratch_dir() as scratch:
        bundle_path = scratch / f"{meta.project}.bundle"
        bundle_path.write_bytes(bundle)

        # Bootstrap: the project doesn't exist here yet.
        if repo is None:
            if meta.bundle_mode != "full":
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
            with reporter.step(3, 5, "Creating repository"):
                git.clone_from_bundle(bundle_path, dest, meta.source_branch)
            _record_import(cfg, meta, dest)
            return ImportResult(
                project=meta.project,
                repo=dest,
                envelope=meta,
                outcome=git.MergeOutcome.FAST_FORWARD,
                commits=git.list_commits(dest, meta.source_branch, limit=50),
                files=[],
                conflicts=[],
                bootstrapped=True,
                merged=True,
            )

        with reporter.step(3, 5, "Verifying bundle"):
            _verify_or_explain(repo, bundle_path, meta)

        with reporter.step(4, 5, "Fetching commits"):
            refs = git.list_bundle_heads(repo, bundle_path)
            src_ref = _pick_source_ref(refs, meta)
            git.fetch_from_bundle(repo, bundle_path, src_ref, git.INCOMING_REF)

            ours = git.current_branch(repo) or "HEAD"
            commits = git.list_commits(repo, f"{ours}..{git.INCOMING_REF}")
            base_for_diff = git.merge_base(repo, ours, git.INCOMING_REF) or ours
            files = git.changed_files(repo, base_for_diff, git.INCOMING_REF)
            preview = git.preview_merge(repo, ours, git.INCOMING_REF)

        if not commits:
            git.delete_ref(repo, git.INCOMING_REF)
            raise NothingToDo(
                f"'{meta.project}' already contains every commit in this package."
            )

        if not do_merge:
            reporter.show_commits(commits, f"{len(commits)} incoming commit(s)")
            reporter.info(
                f"Fetched into {git.INCOMING_REF}. Merge it yourself with:\n"
                f"    git -C {repo} merge {git.INCOMING_REF}"
            )
            return ImportResult(
                meta.project, repo, meta, git.MergeOutcome.ALREADY_UP_TO_DATE,
                commits, files, [], merged=False,
            )

        if not assume_yes:
            reporter.show_commits(commits, f"{len(commits)} incoming commit(s)")
            reporter.show_files(files, "Files affected")
            if preview.clean:
                reporter.info("This will merge cleanly.")
            elif preview.conflicts:
                reporter.warn(
                    "Conflicts predicted",
                    [
                        "Merging will produce conflicts in:",
                        *(f"  {p}" for p in preview.conflicts[:20]),
                        "",
                        "You will be able to resolve them with normal git tools.",
                    ],
                )
            if not reporter.confirm(f"Merge into '{ours}'?", default=True):
                git.delete_ref(repo, git.INCOMING_REF)
                raise UserAbort("Aborted at the merge preview.")

        tree = git.working_tree_status(repo)
        if tree.dirty:
            reporter.warn(
                "Uncommitted changes",
                [
                    f"'{repo.name}' has uncommitted work ({tree.summary()}).",
                    "",
                    "Merging on top of a dirty working tree can leave you with a mess",
                    "that is hard to unpick. Commit or stash first.",
                ],
            )
            if not assume_yes and not reporter.confirm("Merge anyway?", default=False):
                git.delete_ref(repo, git.INCOMING_REF)
                raise UserAbort("Aborted because of uncommitted changes.")

        with reporter.step(5, 5, "Merging"):
            outcome, output = git.merge_ref(
                repo,
                git.INCOMING_REF,
                message=(
                    f"air-sync: merge {len(commits)} commit(s) from "
                    f"{meta.created_by} ({meta.short(meta.head_sha)})"
                ),
            )
            # Raised inside the step so it reports failure rather than printing
            # "done" and then contradicting itself with a conflict panel.
            if outcome is git.MergeOutcome.CONFLICT:
                conflicts = git.conflicted_files(repo)
                _record_conflict(cfg, meta, repo, conflicts)
                raise MergeConflictDetail(meta, repo, conflicts, commits, files)

    if outcome is git.MergeOutcome.FAILED:
        git.delete_ref(repo, git.INCOMING_REF)
        raise EnvironmentError_(f"The merge failed:\n{output}")

    _record_import(cfg, meta, repo)
    git.delete_ref(repo, git.INCOMING_REF)

    return ImportResult(
        meta.project, repo, meta, outcome, commits, files, [], merged=True
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
    ) -> None:
        super().__init__(f"{len(conflicts)} conflicted file(s) in {repo}")
        self.envelope = meta
        self.repo = repo
        self.conflicts = conflicts
        self.commits = commits
        self.files = files


# ------------------------------------------------------------------------- resolve


def finalize_resolution(repo: Path, project: str, cfg: Config) -> bool:
    """Finish an import that stopped with conflicts. Returns True when complete."""
    remaining = git.conflicted_files(repo)
    if remaining:
        raise MergeConflict(
            f"{len(remaining)} file(s) still have unresolved conflicts:\n"
            + "\n".join(f"  {p}" for p in remaining)
            + "\n\nResolve them, 'git add' each one, then 'git commit'."
        )

    if git.merge_in_progress(repo):
        raise MergeConflict(
            "The conflicts are resolved but the merge is not committed yet.\n"
            f"Run:  git -C {repo} commit"
        )

    state = cfg.project(project)
    pending = state.pending_conflict or {}
    head = pending.get("head_sha")
    if head:
        state.last_synced_commit = head
        state.last_synced_branch = pending.get("source_branch")
        state.last_sync_at = _utc_now()
        state.last_payload_sha256 = pending.get("payload_sha256")
    state.pending_conflict = None
    save(cfg)

    git.delete_ref(repo, git.INCOMING_REF)
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


def _pick_source_ref(refs: dict[str, str], meta: env.Envelope) -> str:
    """Choose which ref to fetch out of the bundle."""
    wanted = f"refs/heads/{meta.source_branch}"
    if wanted in refs:
        return wanted
    for name in refs:
        if name.endswith(f"/{meta.source_branch}"):
            return name
    heads = [n for n in refs if n.startswith("refs/heads/")]
    if heads:
        return heads[0]
    if refs:
        return next(iter(refs))
    raise PayloadError("The bundle contains no refs to fetch.")


def _verify_or_explain(repo: Path, bundle_path: Path, meta: env.Envelope) -> None:
    """Turn ``git bundle verify`` failures into messages that say what to do."""
    if meta.base_sha and not git.rev_exists(repo, meta.base_sha):
        raise PayloadError(
            f"This package is an incremental update that continues from commit "
            f"{meta.base_sha[:7]}, which is not in your copy of '{meta.project}'.\n\n"
            "Either an earlier sync package never arrived, or this repository is not "
            "the one the package was built from.\n\n"
            "Ask Computer A to export the full history instead:\n"
            f"    git-air-sync export {meta.project} --full"
        )

    verification = git.verify_bundle(repo, bundle_path)
    if verification.ok:
        return

    if verification.not_a_bundle:
        raise PayloadError(
            "The document decoded and passed its checksum, but the payload is not a "
            "git bundle. This package was probably produced by an incompatible "
            "version of git-air-sync."
        )

    if verification.missing_prereqs:
        missing = "\n".join(f"    {sha[:12]}" for sha in verification.missing_prereqs[:10])
        raise PayloadError(
            f"Your copy of '{meta.project}' is missing commits this package builds on:\n"
            f"{missing}\n\n"
            "An earlier sync package probably never arrived. Ask Computer A to export "
            "the full history:\n"
            f"    git-air-sync export {meta.project} --full"
        )

    raise PayloadError(f"git could not verify the bundle:\n{verification.raw}")


def _record_import(cfg: Config, meta: env.Envelope, repo: Path) -> None:
    state = cfg.project(meta.project)
    state.path = str(repo)
    state.last_synced_commit = meta.head_sha
    state.last_synced_branch = meta.source_branch
    state.last_sync_at = _utc_now()
    state.last_payload_sha256 = meta.payload_sha256
    state.pending_conflict = None
    save(cfg)


def _record_conflict(
    cfg: Config, meta: env.Envelope, repo: Path, conflicts: list[str]
) -> None:
    state = cfg.project(meta.project)
    state.path = str(repo)
    # last_synced_commit is deliberately NOT advanced: the merge isn't done.
    state.pending_conflict = {
        "head_sha": meta.head_sha,
        "source_branch": meta.source_branch,
        "payload_sha256": meta.payload_sha256,
        "files": conflicts,
        "detected_at": _utc_now(),
    }
    save(cfg)


def _check_disk_space(out_dir: Path, estimated: int) -> None:
    try:
        free = shutil.disk_usage(out_dir).free
    except OSError:
        return
    needed = estimated * 3  # bundle + in-memory document + the .part file
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
