"""All rendering.

Six primitives — ``banner``, ``panel``, ``table``, ``pill``, ``note``, ``step`` — each
with a rich implementation and a stdlib one. Every richer screen (commit previews,
export summaries, the conflict alert) is composed from those six and therefore contains
no conditional logic of its own.
"""

from __future__ import annotations

import shutil
import textwrap
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

from .. import __version__
from . import theme
from .theme import Pill, icon

_MAX_WIDTH = 100


def _width() -> int:
    return min(shutil.get_terminal_size((80, 24)).columns, _MAX_WIDTH)


# ---------------------------------------------------------------------- primitives


def pill(kind: Pill) -> str:
    """``[SUCCESS]`` — coloured when the terminal supports it."""
    if theme.HAS_RICH:
        return f"[{kind.style}]\\[{kind.label}][/]"
    if theme.IS_TTY:
        return f"{kind.ansi}[{kind.label}]{theme.RESET}"
    return f"[{kind.label}]"


def _escape(text: str) -> str:
    """Neutralise square brackets in *data* so rich doesn't read them as markup.

    Commit subjects like ``[hotfix] fix the thing`` are entirely normal, and without
    this rich would silently swallow the bracketed part or raise on a bad tag.
    """
    if not theme.HAS_RICH:
        return text
    from rich.markup import escape

    return escape(text)


def note(text: str, kind: Pill | None = None) -> None:
    prefix = pill(kind) + " " if kind else ""
    console = theme.console()
    if console:
        console.print(prefix + _escape(text))
    else:
        # In plain mode `pill()` already returns rendered text, and no caller passes
        # rich markup, so nothing needs stripping here.
        print(prefix + text)


def banner(role_label: str) -> None:
    title = "git-air-sync"
    subtitle = f"v{__version__}  {icon('bullet')}  {role_label}"
    console = theme.console()
    if console:
        console.print()
        console.print(f"  [bold {theme.ACCENT}]{title}[/]", end="")
        console.print(f"  [dim]{subtitle}[/]")
        console.print(f"  [{theme.ACCENT_ALT}]{icon('h') * min(_width() - 4, 60)}[/]")
        console.print()
    else:
        print()
        print(f"  {title}  {subtitle}")
        print("  " + icon("h") * min(_width() - 4, 60))
        print()


def panel(title: str, lines: Sequence[str], kind: Pill = Pill.INFO) -> None:
    console = theme.console()
    if console:
        from rich.panel import Panel

        body = "\n".join(_escape(line) for line in lines)
        console.print(
            Panel(
                body,
                title=f"[{kind.style}]{_escape(title)}[/]",
                border_style=kind.style.replace("bold ", ""),
                width=_width(),
                padding=(0, 1),
            )
        )
        return

    width = _width()
    inner = width - 4
    print(f"{icon('tl')}{icon('h') * (width - 2)}{icon('tr')}")
    header = f" [{kind.label}] {title} "
    print(f"{icon('v')} {header.ljust(inner)} {icon('v')}")
    print(f"{icon('v')} {'' .ljust(inner)} {icon('v')}")
    for line in lines:
        for wrapped in (textwrap.wrap(line, inner) or [""]):
            print(f"{icon('v')} {wrapped.ljust(inner)} {icon('v')}")
    print(f"{icon('bl')}{icon('h') * (width - 2)}{icon('br')}")


def table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    title: str | None = None,
    styles: Sequence[str] | None = None,
) -> None:
    if not rows:
        note("  (nothing to show)")
        return

    console = theme.console()
    if console:
        from rich.table import Table

        rich_table = Table(
            title=f"[heading]{title}[/]" if title else None,
            title_justify="left",
            header_style=f"bold {theme.ACCENT}",
            border_style="dim",
            width=_width(),
        )
        for index, header in enumerate(headers):
            rich_table.add_column(
                header,
                style=(styles[index] if styles and index < len(styles) else None),
                overflow="fold" if index == len(headers) - 1 else "ellipsis",
                no_wrap=index != len(headers) - 1,
            )
        for row in rows:
            rich_table.add_row(*[_escape(str(cell)) for cell in row])
        console.print(rich_table)
        return

    _plain_table(headers, rows, title)


def _plain_table(
    headers: Sequence[str], rows: Sequence[Sequence[str]], title: str | None
) -> None:
    if title:
        print(f"\n{title}")

    columns = len(headers)
    widths = [len(str(h)) for h in headers]
    for row in rows:
        for i in range(columns):
            widths[i] = max(widths[i], len(str(row[i])) if i < len(row) else 0)

    # Shrink the last column until the table fits the terminal.
    available = _width() - (3 * columns + 1)
    if sum(widths) > available:
        overflow = sum(widths) - available
        widths[-1] = max(12, widths[-1] - overflow)

    def rule() -> str:
        return "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def render(cells: Sequence[str]) -> str:
        out = []
        for i, width in enumerate(widths):
            text = str(cells[i]) if i < len(cells) else ""
            if len(text) > width:
                text = textwrap.shorten(text, width, placeholder="…")
            out.append(" " + text.ljust(width) + " ")
        return "|" + "|".join(out) + "|"

    print(rule())
    print(render(headers))
    print(rule())
    for row in rows:
        print(render(row))
    print(rule())


@contextmanager
def step(index: int, total: int, text: str) -> Iterator[None]:
    """``[2/4] Creating patch series`` — a spinner when possible, plain lines otherwise."""
    label = f"[{index}/{total}] {text}"
    console = theme.console()

    if console and theme.IS_TTY:
        with console.status(f"[{theme.ACCENT}]{label}[/]", spinner="dots"):
            yield
        console.print(f"  [ok]{icon('ok')}[/] [dim]{label}[/]")
        return

    # No newline yet, so the result lands on the same line. Nothing prints between
    # enter and exit — prompts and warnings are always raised outside a step.
    print(f"{label}... ", end="", flush=True)
    try:
        yield
    except Exception:
        print(f"{icon('err')} failed", flush=True)
        raise
    print(f"{icon('ok')} done", flush=True)


# ------------------------------------------------------------------- compositions


def commit_table(commits: Sequence[Any], title: str) -> None:
    rows = [
        (c.short, _truncate(c.author, 18), _date(c.date), c.subject) for c in commits[:50]
    ]
    table(
        ["Commit", "Author", "Date", "Subject"],
        rows,
        title=title,
        styles=["sha", "muted", "muted", None],
    )
    if len(commits) > 50:
        note(f"  … and {len(commits) - 50} more")


def file_table(files: Sequence[Any], title: str) -> None:
    if not files:
        return
    label = {
        "A": "added",
        "M": "modified",
        "D": "deleted",
        "R": "renamed",
        "C": "copied",
        "T": "type",
    }
    rows = [(label.get(f.status, f.status), f.label) for f in files[:50]]
    table(["Change", "File"], rows, title=title, styles=["muted", None])
    if len(files) > 50:
        note(f"  … and {len(files) - 50} more")


def export_summary(result: Any) -> None:
    plan = result.plan
    from ..core.sync import human_size

    base = plan.base[:7] if plan.base else "(full history)"
    commits_line = f"Commits        {result.commit_count}"
    if result.commit_count != plan.commit_count:
        commits_line += f"  ({plan.commit_count - result.commit_count} excluded)"
    lines = [
        f"Project        {result.project}",
        f"Branch         {plan.branch}",
        commits_line,
        f"Range          {base} {icon('arrow')} {plan.head[:7]}",
        f"Patch series   {human_size(result.patch_bytes)}",
        f"Document       {human_size(result.docx_bytes)}  ({result.ratio:.2f}x)",
        f"Checksum       {result.payload_sha256[:32]}…",
        "",
        f"Written to     {result.path}",
        "",
        "Transfer this file as-is. Do NOT open it in Word and do not let a mail",
        "client 'clean up' the attachment — reflowing the paragraphs destroys the",
        "payload, and it cannot be recovered.",
    ]
    panel("Export complete", lines, Pill.SUCCESS)


def conflict_panel(
    repo: Any, conflicts: Sequence[str], project: str, auto_resolved: Sequence[str] = ()
) -> None:
    lines = [
        f"Applying the patch series stopped with {len(conflicts)} conflicted file(s):",
        "",
        *(f"  {icon('bullet')} {path}" for path in conflicts[:20]),
    ]
    if len(conflicts) > 20:
        lines.append(f"  … and {len(conflicts) - 20} more")
    if auto_resolved:
        lines += [
            "",
            f"{len(auto_resolved)} other file(s) matched an exclude pattern and were",
            "auto-resolved by keeping your local version — they are NOT part of the",
            "conflict below and need no action:",
            "",
            *(f"  {icon('bullet')} {path}" for path in auto_resolved[:20]),
        ]
    lines += [
        "",
        "Resolve them the normal way:",
        "",
        f"  1.  cd {repo}",
        "  2.  git status              # see what needs attention",
        "  3.  edit each file, keeping the changes you want",
        "  4.  git add <file>          # mark each one resolved",
        "",
        "Then finish and record the sync:",
        "",
        f"  git-air-sync resolve {project}",
        "",
        "That runs 'git am --continue' for you — no separate commit needed. Your",
        "recorded sync position has NOT been advanced, so nothing is lost if you",
        "abort instead with:  git am --abort",
    ]
    panel("Patch conflict", lines, Pill.CONFLICT)


def sync_status_table(cfg: Any) -> None:
    rows = []
    for name, state in sorted(cfg.projects.items()):
        if state.pending_conflict:
            status = "CONFLICT"
        elif state.last_synced_commit:
            status = "SYNCED"
        else:
            status = "PENDING"
        rows.append(
            (
                name,
                status,
                (state.last_synced_commit or "—")[:7],
                state.last_synced_branch or "—",
                (state.last_sync_at or "—")[:10],
            )
        )
    table(
        ["Project", "Status", "Last synced", "Branch", "When"],
        rows,
        title="Sync state",
        styles=[None, None, "sha", "muted", "muted"],
    )


# ----------------------------------------------------------------------- helpers


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _date(value: str) -> str:
    return value[:10] if value else "—"
