"""Capability detection, palette, and icons — detected exactly once.

Computer B may not have rich or questionary installed. Rather than scattering
``try: import rich`` through the codebase, every optional dependency is probed here
and exposed as a boolean flag.
"""

from __future__ import annotations

import os
import sys
from enum import Enum

try:  # pragma: no cover - depends on the machine
    from rich.console import Console
    from rich.theme import Theme

    HAS_RICH = True
except ImportError:  # pragma: no cover
    Console = None  # type: ignore[assignment]
    Theme = None  # type: ignore[assignment]
    HAS_RICH = False

try:  # pragma: no cover - depends on the machine
    import questionary

    HAS_QUESTIONARY = True
except ImportError:  # pragma: no cover
    questionary = None  # type: ignore[assignment]
    HAS_QUESTIONARY = False


# AIR_SYNC_PLAIN forces the fallback path. It is how the test-suite exercises
# plain mode on a machine that has rich installed, and a user escape hatch.
if os.environ.get("AIR_SYNC_PLAIN"):
    HAS_RICH = False
    HAS_QUESTIONARY = False

IS_TTY = sys.stdout.isatty()
COLOR = HAS_RICH and IS_TTY and not os.environ.get("NO_COLOR")
UNICODE = (sys.stdout.encoding or "").lower().startswith("utf") and not os.environ.get(
    "AIR_SYNC_ASCII"
)

ACCENT = "bright_cyan"
ACCENT_ALT = "bright_magenta"

PALETTE = {
    "accent": ACCENT,
    "accent.alt": ACCENT_ALT,
    "muted": "dim",
    "path": "dim",
    "sha": "dim yellow",
    "ok": "bold green",
    "warn": "bold yellow",
    "err": "bold red",
    "heading": f"bold {ACCENT}",
}


class Pill(Enum):
    """Status badges. ``(label, rich style, raw ANSI)``."""

    SUCCESS = ("SUCCESS", "bold green", "\033[1;32m")
    SYNCED = ("SYNCED", "bold cyan", "\033[1;36m")
    PENDING = ("PENDING", "bold yellow", "\033[1;33m")
    CONFLICT = ("CONFLICT", "bold red", "\033[1;31m")
    INFO = ("INFO", "bold blue", "\033[1;34m")
    ERROR = ("ERROR", "bold red", "\033[1;31m")

    @property
    def label(self) -> str:
        return self.value[0]

    @property
    def style(self) -> str:
        return self.value[1]

    @property
    def ansi(self) -> str:
        return self.value[2]


RESET = "\033[0m"
DIM = "\033[2m"

_ICONS_UNICODE = {
    "ok": "✓",
    "warn": "⚠",
    "err": "✗",
    "arrow": "→",
    "bullet": "•",
    "h": "─",
    "v": "│",
    "tl": "╭",
    "tr": "╮",
    "bl": "╰",
    "br": "╯",
}

_ICONS_ASCII = {
    "ok": "OK",
    "warn": "!",
    "err": "X",
    "arrow": "->",
    "bullet": "-",
    "h": "-",
    "v": "|",
    "tl": "+",
    "tr": "+",
    "bl": "+",
    "br": "+",
}


def icon(name: str) -> str:
    return (_ICONS_UNICODE if UNICODE else _ICONS_ASCII)[name]


_console = None


def console():
    """The rich Console singleton, or ``None`` in plain mode."""
    global _console
    if not HAS_RICH:
        return None
    if _console is None:
        _console = Console(theme=Theme(PALETTE), highlight=False)
    return _console


def dim(text: str) -> str:
    """Raw-ANSI dim, for the plain path where rich markup means nothing."""
    return f"{DIM}{text}{RESET}" if COLOR or (IS_TTY and not os.environ.get("NO_COLOR")) else text
