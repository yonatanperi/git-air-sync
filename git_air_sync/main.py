"""Command-line entry point."""

from __future__ import annotations

import signal
import sys
from pathlib import Path

import click

from . import __version__, config as config_mod
from .cli import displays, prompts
from .cli.prompts import Choice
from .cli.reporter import ConsoleReporter
from .cli.theme import Pill
from .config import Config
from .core import git_ops as git, sync
from .errors import (
    EXIT_INTERRUPTED,
    AirSyncError,
    EnvironmentError_,
    NothingToDo,
    UserAbort,
)

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}

# Sentinel for a "(back)" menu choice — distinct from every real value a select()
# might return, including None (which some menus use for "no default"/"clear").
_BACK = object()


# ------------------------------------------------------------------------- helpers


def _fail(message: str, kind: Pill = Pill.ERROR, title: str = "Error") -> None:
    displays.panel(title, str(message).splitlines(), kind)


def _load_config() -> Config:
    try:
        return config_mod.load()
    except config_mod.ConfigError as exc:
        _fail(str(exc), title="Configuration")
        raise SystemExit(EnvironmentError_.exit_code)


def _require_git() -> None:
    try:
        git.git_executable()
    except git.GitMissingError as exc:
        _fail(
            str(exc)
            + "\n\nInstall git, or add it to PATH, then run this command again.",
            title="git not found",
        )
        raise SystemExit(EnvironmentError_.exit_code)


def _check_role(cfg: Config, expected: str, force: bool, action: str) -> None:
    if cfg.machine_role in (None, expected) or force:
        return
    other = "import" if expected == "A" else "export"
    _fail(
        f"This machine is configured as {cfg.role_label}, but {action} is a "
        f"Computer {expected} operation.\n\n"
        f"If that is deliberate, pass --force-role. If the role is simply wrong, fix "
        f"it with 'git-air-sync config'. Otherwise you probably meant to {other}.",
        Pill.PENDING,
        title="Wrong machine role",
    )
    raise SystemExit(EnvironmentError_.exit_code)


def _pick_project(
    cfg: Config, name: str | None, assume_yes: bool = False
) -> tuple[str, Path]:
    """Resolve a project name to a repo, prompting when not given."""
    if name:
        path = cfg.project_path(name)
        if path is None or not path.exists():
            root = config_mod.resolve_path(cfg.projects_root)
            raise EnvironmentError_(
                f"No project named '{name}' under {root}."
            )
        if not git.is_git_repo(path):
            raise EnvironmentError_(f"{path} is not a git repository.")
        return name, path

    root = config_mod.resolve_path(cfg.projects_root)
    if root is None or not root.exists():
        raise EnvironmentError_(
            "No projects root is configured. Run 'git-air-sync config' first."
        )

    repos = git.discover_repos(root)
    if not repos:
        raise EnvironmentError_(f"No git repositories found under {root}.")

    def _last_used(repo: Path) -> str:
        state = cfg.projects.get(repo.name)
        return (state.last_sync_at if state else None) or ""

    repos = sorted(repos, key=_last_used, reverse=True)

    default = cfg.default_project
    if default and any(r.name == default for r in repos):
        chosen = next(r for r in repos if r.name == default)
        # --yes means "don't ask me anything", so take the default rather than
        # prompting — otherwise scripted runs fail on a non-interactive terminal.
        if assume_yes or prompts.confirm(
            f"Use default project '{default}'?", default=True
        ):
            return default, chosen

    choices = [
        Choice(
            value=repo,
            label=repo.name,
            description=_project_hint(cfg, repo.name),
            is_default=repo.name == default,
        )
        for repo in repos
    ]
    chosen = prompts.select("Which project?", choices, flag="PROJECT")
    return chosen.name, chosen


def _project_hint(cfg: Config, name: str) -> str:
    state = cfg.projects.get(name)
    if not state or not state.last_synced_commit:
        return "never synced"
    if state.pending_conflict:
        return "conflict pending"
    return f"synced at {state.last_synced_commit[:7]}"


def _pick_document(cfg: Config, given: str | None, assume_yes: bool = False) -> Path:
    if given:
        path = Path(given).expanduser()
        if not path.is_file():
            raise EnvironmentError_(f"{path} does not exist.")
        return path

    inbox = cfg.inbox_dir()
    candidates = sorted(
        (p for p in inbox.glob("*.docx") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ) if inbox.exists() else []

    if not candidates:
        return prompts.path_prompt(
            f"No .docx files found in {inbox}. Path to the package",
            validate=lambda v: (
                (True, "") if Path(v).expanduser().is_file() else (False, "Not a file.")
            ),
            flag="DOCX",
        )

    if assume_yes:
        return candidates[0]  # newest first

    choices = [
        Choice(
            value=path,
            label=path.name,
            description=f"{sync.human_size(path.stat().st_size)}",
            is_default=index == 0,
        )
        for index, path in enumerate(candidates)
    ]
    return prompts.select("Which package?", choices, default=candidates[0], flag="DOCX")


# --------------------------------------------------------------------------- group


@click.group(context_settings=CONTEXT_SETTINGS, invoke_without_command=True)
@click.version_option(__version__, "-V", "--version", prog_name="git-air-sync")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Sync git repositories across an air gap using .docx files."""
    sync.sweep_stale_scratch()
    if ctx.invoked_subcommand is None:
        ctx.invoke(menu)


@cli.command()
def menu() -> None:
    """Interactive main menu (the default when run with no arguments)."""
    cfg = _load_config()

    if not config_mod.exists():
        displays.banner("FIRST RUN")
        cfg = _run_wizard(cfg)
    else:
        displays.banner(cfg.role_label)

    _require_git()

    command_map = {
        "export": export,
        "import": import_cmd,
        "status": status,
        "resolve": resolve,
        "config": config_cmd,
        "doctor": doctor,
    }

    # Loop so that finishing (or backing out of) any submenu returns here instead
    # of ending the process — only "quit" actually exits.
    while True:
        entries: list[Choice] = []
        if cfg.machine_role == "B":
            entries.append(Choice("import", "Import a package", "decode a .docx and apply it"))
        elif cfg.machine_role == "A":
            entries.append(Choice("export", "Export a package", "package commits into a .docx"))
        else:
            entries.append(Choice("export", "Export a package", "package commits into a .docx"))
            entries.append(Choice("import", "Import a package", "decode a .docx and apply it"))
        entries += [
            Choice("status", "Show sync status", "what has crossed the gap"),
            Choice("resolve", "Finish a conflicted import", ""),
            Choice("config", "Settings", ""),
            Choice("doctor", "Check this machine", ""),
            Choice("quit", "Quit", ""),
        ]

        action = prompts.select("What would you like to do?", entries)

        if action == "quit":
            return

        ctx = click.get_current_context()
        ctx.invoke(command_map[action])
        cfg = _load_config()  # a settings change may have altered the role/menu


# -------------------------------------------------------------------------- export


@cli.command()
@click.argument("project", required=False)
@click.option("--full", "force_full", is_flag=True, help="Export the entire history.")
@click.option("--base", "base_override", help="Export commits after this commit.")
@click.option("--out", "out_dir", type=click.Path(), help="Where to write the .docx.")
@click.option("--yes", "-y", "assume_yes", is_flag=True, help="Skip all confirmations.")
@click.option("--force-role", is_flag=True, help="Run even if this machine is role B.")
def export(
    project: str | None,
    force_full: bool,
    base_override: str | None,
    out_dir: str | None,
    assume_yes: bool,
    force_role: bool,
) -> None:
    """Package new commits into a .docx for transfer (Computer A)."""
    cfg = _load_config()
    _require_git()
    _check_role(cfg, "A", force_role, "export")
    displays.banner(cfg.role_label)

    name, repo = _pick_project(cfg, project, assume_yes)
    state = cfg.project(name)
    state.path = str(repo)

    destination = Path(out_dir).expanduser() if out_dir else cfg.output_dir()

    result = sync.export_project(
        repo,
        name,
        state,
        destination,
        ConsoleReporter(),
        force_full=force_full,
        base_override=base_override,
        assume_yes=assume_yes,
        max_payload_mb=cfg.max_payload_mb,
        export_refs=cfg.export_refs,
        exclude_patterns=cfg.effective_exclude_patterns(name),
    )

    state.last_synced_commit = result.plan.head
    state.last_synced_branch = result.plan.branch
    state.last_sync_at = sync._utc_now()
    state.last_payload_sha256 = result.payload_sha256
    config_mod.save(cfg)

    displays.export_summary(result)


# -------------------------------------------------------------------------- import


@cli.command("import")
@click.argument("docx", required=False)
@click.option("--project", help="Override the project named in the package.")
@click.option("--repo", "repo_path", type=click.Path(), help="Apply into this repo.")
@click.option("--no-merge", is_flag=True, help="Save the patch series only; do not apply it.")
@click.option("--yes", "-y", "assume_yes", is_flag=True, help="Skip all confirmations.")
@click.option("--force-role", is_flag=True, help="Run even if this machine is role A.")
def import_cmd(
    docx: str | None,
    project: str | None,
    repo_path: str | None,
    no_merge: bool,
    assume_yes: bool,
    force_role: bool,
) -> None:
    """Decode a .docx package and apply it to a repository (Computer B)."""
    cfg = _load_config()
    _require_git()
    _check_role(cfg, "B", force_role, "import")
    displays.banner(cfg.role_label)

    document = _pick_document(cfg, docx, assume_yes)
    override = Path(repo_path).expanduser() if repo_path else None
    if override is None and project:
        override = cfg.project_path(project)

    try:
        result = sync.import_document(
            document,
            cfg,
            ConsoleReporter(),
            repo_override=override,
            do_merge=not no_merge,
            assume_yes=assume_yes,
        )
    except sync.MergeConflictDetail as conflict:
        displays.conflict_panel(
            conflict.repo, conflict.conflicts, conflict.envelope.project, conflict.auto_resolved
        )
        raise SystemExit(conflict.exit_code)

    meta = result.envelope
    verb = "Created" if result.bootstrapped else ("Applied" if result.applied else "Saved")
    lines = [
        f"Project        {result.project}",
        f"Repository     {result.repo}",
        f"Branch         {meta.source_branch}",
        f"Commits        {len(result.commits)}",
    ]
    if result.local_head:
        lines.append(f"Now at         {result.local_head[:7]}")
    if result.auto_resolved:
        lines.append(
            f"Kept local     {len(result.auto_resolved)} excluded file(s), not overwritten"
        )
    lines += [
        f"From           {meta.created_by} on {meta.created_at[:10]}",
        "",
        f"{verb} successfully.",
    ]
    displays.panel("Import complete", lines, Pill.SUCCESS)


# ------------------------------------------------------------------------- resolve


@cli.command()
@click.argument("project", required=False)
def resolve(project: str | None) -> None:
    """Finish an import that stopped with patch conflicts."""
    cfg = _load_config()
    _require_git()
    displays.banner(cfg.role_label)

    pending = {
        name: state
        for name, state in cfg.projects.items()
        if state.pending_conflict
    }
    if not pending:
        displays.panel(
            "Nothing to resolve",
            ["No import is waiting on conflict resolution."],
            Pill.SYNCED,
        )
        return

    if project is None:
        if len(pending) == 1:
            project = next(iter(pending))
        else:
            choices = [Choice(name, name) for name in sorted(pending)] + [
                Choice(_BACK, "(back)")
            ]
            selection = prompts.select("Which project?", choices, flag="PROJECT")
            if selection is _BACK:
                return
            project = selection

    state = cfg.projects.get(project)
    if not state or not state.pending_conflict:
        raise EnvironmentError_(f"'{project}' has no pending conflict.")

    repo = config_mod.resolve_path(state.path)
    if repo is None or not repo.exists():
        raise EnvironmentError_(f"Cannot find the repository for '{project}'.")

    sync.finalize_resolution(repo, project, cfg)
    displays.panel(
        "Resolution recorded",
        [
            f"'{project}' is now synced at "
            f"{(state.last_synced_commit or '')[:7]}.",
            "",
            "The pending conflict has been cleared.",
        ],
        Pill.SUCCESS,
    )


# -------------------------------------------------------------------------- status


@cli.command()
@click.argument("project", required=False)
def status(project: str | None) -> None:
    """Show what has crossed the gap so far."""
    cfg = _load_config()
    displays.banner(cfg.role_label)

    if not cfg.projects:
        displays.panel(
            "No sync history",
            ["Nothing has been exported or imported from this machine yet."],
            Pill.PENDING,
        )
        return

    displays.sync_status_table(cfg)

    # On the receiving machine, local commits are the precursor to every future
    # conflict, so say so before it becomes a surprise.
    if cfg.machine_role == "B":
        _require_git()
        for name, state in sorted(cfg.projects.items()):
            if project and name != project:
                continue
            repo = config_mod.resolve_path(state.path)
            if not repo or not repo.exists() or not state.last_import_head:
                continue
            if not git.rev_exists(repo, state.last_import_head):
                continue
            branch = git.current_branch(repo)
            if branch and git.resolve_sha(repo, branch) != state.last_import_head:
                ahead = git.count_commits(repo, f"{state.last_import_head}..{branch}")
                if ahead:
                    displays.panel(
                        f"Local commits in '{name}'",
                        [
                            f"'{name}' has {ahead} commit(s) that did not arrive in a "
                            "sync package.",
                            "",
                            "This machine is configured as import-only, so those commits "
                            "cannot travel back to Computer A, and they are what will "
                            "cause patch conflicts on the next import.",
                        ],
                        Pill.PENDING,
                    )


# -------------------------------------------------------------------------- config


@cli.command("config")
def config_cmd() -> None:
    """Change settings and inspect recorded sync positions."""
    cfg = _load_config()
    displays.banner(cfg.role_label)

    while True:
        displays.panel(
            "Current settings",
            [
                f"Config file      {config_mod.config_path()}",
                f"Machine role     {cfg.role_label}",
                f"Projects root    {cfg.projects_root or '(not set)'}",
                f"Drop folder      {cfg.drop_folder or '(not set)'}",
                f"Output folder    {cfg.export_output_dir or '(same as drop folder)'}",
                f"Default project  {cfg.default_project or '(none)'}",
                f"Size warning     {cfg.max_payload_mb} MB",
                f"Refs exported    {cfg.export_refs}",
                f"Exclude patterns {len(cfg.exclude_patterns)} global",
            ],
            Pill.INFO,
        )

        action = prompts.select(
            "What would you like to change?",
            [
                Choice("root", "Projects root folder"),
                Choice("drop", "Drop folder (where packages arrive)"),
                Choice("out", "Output folder (where exports are written)"),
                Choice("default", "Default project"),
                Choice("role", "Machine role"),
                Choice("size", "Size warning threshold"),
                Choice("refs", "Which refs to export"),
                Choice("exclude", "Exclude patterns (files never synced across the air gap)"),
                Choice("hashes", "Inspect / override sync positions"),
                Choice(_BACK, "(back)"),
            ],
        )

        if action is _BACK:
            return
        if action == "root":
            cfg.projects_root = str(
                prompts.path_prompt(
                    "Projects root folder",
                    default=cfg.projects_root or str(Path.home()),
                    validate=config_mod.validate_projects_root,
                )
            )
        elif action == "drop":
            cfg.drop_folder = str(
                prompts.path_prompt(
                    "Drop folder",
                    default=cfg.drop_folder or str(Path.home() / "AirSyncDrop"),
                    validate=config_mod.validate_writable_dir,
                )
            )
        elif action == "out":
            cfg.export_output_dir = str(
                prompts.path_prompt(
                    "Output folder",
                    default=cfg.export_output_dir or cfg.drop_folder or "",
                    validate=config_mod.validate_writable_dir,
                )
            )
        elif action == "default":
            choice = _choose_default_project(cfg)
            if choice is _BACK:
                continue
            cfg.default_project = choice
        elif action == "role":
            choice = prompts.select(
                "This machine is:",
                [
                    Choice("A", "Computer A — internet-connected, exports packages"),
                    Choice("B", "Computer B — air-gapped, imports packages"),
                    Choice(_BACK, "(back)"),
                ],
                default=cfg.machine_role,
            )
            if choice is _BACK:
                continue
            cfg.machine_role = choice
        elif action == "size":
            cfg.max_payload_mb = int(
                prompts.text(
                    "Warn when a document exceeds (MB)",
                    default=str(cfg.max_payload_mb),
                    validate=lambda v: (
                        (True, "") if v.isdigit() and int(v) > 0 else (False, "Enter a positive number.")
                    ),
                )
            )
        elif action == "refs":
            choice = prompts.select(
                "Which refs should an export include?",
                [
                    Choice("branch", "Current branch only", "smaller packages"),
                    Choice("all", "All branches and tags", "larger, but complete"),
                    Choice(_BACK, "(back)"),
                ],
                default=cfg.export_refs,
            )
            if choice is _BACK:
                continue
            cfg.export_refs = choice
        elif action == "exclude":
            _edit_exclude_patterns(cfg)
        elif action == "hashes":
            _edit_hashes(cfg)

        config_mod.save(cfg)


def _choose_default_project(cfg: Config) -> object:
    """Returns the chosen project name, ``None`` for "no default", or ``_BACK``."""
    root = config_mod.resolve_path(cfg.projects_root)
    if root is None or not root.exists():
        raise EnvironmentError_("Set the projects root first.")
    repos = git.discover_repos(root)
    if not repos:
        raise EnvironmentError_(f"No git repositories found under {root}.")
    choices = (
        [Choice(None, "(no default)")]
        + [Choice(r.name, r.name, is_default=r.name == cfg.default_project) for r in repos]
        + [Choice(_BACK, "(back)")]
    )
    return prompts.select("Default project", choices, default=cfg.default_project)


def _edit_exclude_patterns(cfg: Config) -> None:
    """Global patterns, plus a per-project addition list — see
    Config.effective_exclude_patterns for how the two combine."""
    while True:
        scope = prompts.select(
            "Exclude patterns",
            [
                Choice("global", "Edit the global list", "applies to every project"),
                Choice("project", "Edit one project's additions", "on top of the global list"),
                Choice(_BACK, "(back)"),
            ],
        )
        if scope is _BACK:
            return
        if scope == "global":
            _edit_pattern_list(cfg.exclude_patterns, "global exclude patterns")
            config_mod.save(cfg)
        else:
            if not cfg.projects:
                displays.note("No projects recorded yet.", Pill.PENDING)
                continue
            name = prompts.select(
                "Which project?",
                [Choice(n, n) for n in sorted(cfg.projects)] + [Choice(_BACK, "(back)")],
            )
            if name is _BACK:
                continue
            state = cfg.project(name)
            _edit_pattern_list(state.exclude_patterns, f"'{name}' additions")
            config_mod.save(cfg)


def _edit_pattern_list(patterns: list[str], label: str) -> None:
    """Mutates ``patterns`` in place via an add/remove submenu."""
    while True:
        displays.panel(
            f"Exclude patterns ({label})",
            [f"  - {p}" for p in patterns] or ["  (none)"],
            Pill.INFO,
        )
        action = prompts.select(
            "Exclude patterns",
            [
                Choice("add", "Add a pattern"),
                Choice("remove", "Remove a pattern"),
                Choice(_BACK, "(back)"),
            ],
        )
        if action is _BACK:
            return
        if action == "add":
            pattern = prompts.text(
                "Pattern (gitignore-style, e.g. CLAUDE.md, .claude/**, docs/*.local.md)"
            ).strip()
            if pattern and pattern not in patterns:
                patterns.append(pattern)
        elif action == "remove":
            if not patterns:
                continue
            choices = [Choice(p, p) for p in patterns] + [Choice(_BACK, "(back)")]
            selection = prompts.select("Remove which pattern?", choices)
            if selection is not _BACK:
                patterns.remove(selection)


def _edit_hashes(cfg: Config) -> None:
    if not cfg.projects:
        displays.panel("No sync history", ["Nothing recorded yet."], Pill.PENDING)
        return

    displays.sync_status_table(cfg)

    name = prompts.select(
        "Which project?",
        [Choice(n, n, _project_hint(cfg, n)) for n in sorted(cfg.projects)]
        + [Choice(None, "(back)")],
    )
    if name is None:
        return

    state = cfg.project(name)
    repo = config_mod.resolve_path(state.path)
    branch = git.current_branch(repo) if repo and repo.exists() else None

    options = []
    if repo and repo.exists() and branch:
        options.append(Choice("pick", "Pick from recent commits", "same as a first-time export"))
    options.append(Choice("manual", "Enter a commit hash"))
    if state.last_synced_commit:
        options.append(Choice("clear", "Clear (mark as never synced)"))
    options.append(Choice(None, "(back)"))

    action = prompts.select(f"Last synced commit for '{name}'", options)
    if action is None:
        return

    if action == "clear":
        new_value = ""
    elif action == "pick":
        commits = git.list_commits(repo, branch, limit=50)
        if not commits:
            displays.note(f"'{name}' has no commits to choose from.", Pill.PENDING)
            return
        new_value = ConsoleReporter().choose_commit(
            commits, f"Last synced commit for '{name}'"
        )
    else:

        def validate(value: str) -> tuple[bool, str]:
            if value.strip() in ("", "-", "none"):
                return True, ""
            if repo and repo.exists() and not git.rev_exists(repo, value.strip()):
                return False, f"{value.strip()[:12]} is not a commit in {repo.name}."
            return True, ""

        new_value = prompts.text(
            f"Last synced commit for '{name}' (blank or '-' to clear)",
            default=state.last_synced_commit or "",
            validate=validate,
        ).strip()

    state.last_synced_commit = None if new_value in ("", "-", "none") else new_value
    if state.last_synced_commit and repo and repo.exists():
        state.last_synced_commit = git.resolve_sha(repo, state.last_synced_commit)
    config_mod.save(cfg)
    displays.note(f"Updated '{name}'.", Pill.SUCCESS)


# -------------------------------------------------------------------------- doctor


@cli.command()
def doctor() -> None:
    """Check that this machine can run git-air-sync."""
    cfg = _load_config()
    displays.banner(cfg.role_label)

    from .cli import theme

    rows: list[tuple[str, str, str]] = []

    py_ok = sys.version_info >= (3, 10)
    rows.append(
        (
            "Python",
            "OK" if py_ok else "TOO OLD",
            f"{sys.version_info.major}.{sys.version_info.minor}"
            + ("" if py_ok else " — click 8.3 needs 3.10+"),
        )
    )

    try:
        version = git.git_version()
        parts = version.split()[2].split(".")
        modern = (int(parts[0]), int(parts[1])) >= (2, 28)
        rows.append(
            (
                "git",
                "OK" if modern else "OLD",
                version + ("" if modern else " — bootstrapping a repo needs 2.28+"),
            )
        )
    except (git.GitMissingError, IndexError, ValueError):
        rows.append(("git", "MISSING", "not on PATH — nothing will work"))

    rows.append(
        (
            "rich",
            "OK" if theme.HAS_RICH else "ABSENT",
            "formatted output" if theme.HAS_RICH else "falling back to plain text",
        )
    )
    rows.append(
        (
            "questionary",
            "OK" if theme.HAS_QUESTIONARY else "ABSENT",
            "interactive menus"
            if theme.HAS_QUESTIONARY
            else "falling back to numbered menus",
        )
    )
    rows.append(
        (
            "Config",
            "OK" if config_mod.exists() else "MISSING",
            str(config_mod.config_path()),
        )
    )

    root = config_mod.resolve_path(cfg.projects_root)
    rows.append(
        (
            "Projects root",
            "OK" if root and root.is_dir() else "MISSING",
            str(root or "(not set)"),
        )
    )

    out = cfg.output_dir()
    try:
        import shutil as _shutil

        free = _shutil.disk_usage(out if out.exists() else out.parent).free
        rows.append(("Free space", "OK", f"{sync.human_size(free)} in {out}"))
    except OSError:
        rows.append(("Free space", "UNKNOWN", str(out)))

    displays.table(["Check", "Status", "Detail"], rows, title="Environment")


# --------------------------------------------------------------------------- setup


def _run_wizard(cfg: Config) -> Config:
    displays.panel(
        "Welcome",
        [
            "git-air-sync moves real git history between an internet-connected",
            "machine and an air-gapped one, inside a .docx file.",
            "",
            "A few questions, once.",
        ],
        Pill.INFO,
    )

    cfg.machine_role = prompts.select(
        "Which machine is this?",
        [
            Choice("A", "Computer A", "internet-connected — creates packages"),
            Choice("B", "Computer B", "air-gapped — receives packages"),
        ],
    )

    cfg.projects_root = str(
        prompts.path_prompt(
            "Where do your git repositories live?",
            default=str(Path.home() / "Desktop" / "projects"),
            validate=config_mod.validate_projects_root,
        )
    )

    label = (
        "Where should exported packages be written?"
        if cfg.machine_role == "A"
        else "Where do incoming packages arrive?"
    )
    folder = prompts.path_prompt(
        label,
        default=str(Path.home() / ("AirSyncOut" if cfg.machine_role == "A" else "AirSyncDrop")),
        validate=config_mod.validate_writable_dir,
    )
    folder.mkdir(parents=True, exist_ok=True)
    cfg.drop_folder = str(folder)

    config_mod.save(cfg)
    displays.note(f"Saved to {config_mod.config_path()}", Pill.SUCCESS)
    return cfg


@cli.command()
def init() -> None:
    """Re-run the first-time setup wizard."""
    cfg = _load_config()
    displays.banner(cfg.role_label)
    _run_wizard(cfg)


# ---------------------------------------------------------------------------- main


def main() -> int:
    # SIGTERM would otherwise kill us without unwinding, stranding a large payload in
    # the temp directory. Turning it into SystemExit lets `finally` and atexit run.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(EXIT_INTERRUPTED))

    try:
        cli.main(standalone_mode=False)
        return 0
    except NothingToDo as exc:
        displays.note(str(exc), Pill.SYNCED)
        return exc.exit_code
    except UserAbort as exc:
        displays.note(str(exc), Pill.PENDING)
        return exc.exit_code
    except AirSyncError as exc:
        _fail(str(exc))
        return exc.exit_code
    except git.GitError as exc:
        _fail(str(exc), title="git failed")
        return EnvironmentError_.exit_code
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    except click.exceptions.Exit as exc:
        # --help and --version reach here because standalone_mode is off.
        return int(exc.exit_code)
    except click.exceptions.Abort:
        displays.note("Cancelled.", Pill.PENDING)
        return EXIT_INTERRUPTED
    except KeyboardInterrupt:
        displays.note("Cancelled — temporary files removed.", Pill.PENDING)
        return EXIT_INTERRUPTED
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        sync.cleanup_all()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
