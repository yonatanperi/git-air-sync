"""Gitignore-style exclude pattern matching.

Shared by export-time pathspec construction (:func:`git_ops.format_patch`) and
import-time conflict-path matching (``sync._apply_with_auto_resolve``), so a
file excluded from the transmitted patch series and a file auto-resolved on
conflict are governed by exactly the same rule — a user who excludes
"CLAUDE.md" gets one consistent behaviour, not two subtly different ones.

Semantics deliberately mirror ``.gitignore``:
  - no ``/``       matches the name at any depth ("CLAUDE.md" matches both
                    "CLAUDE.md" and "sub/dir/CLAUDE.md")
  - has ``/``      anchored to the repo root
  - ``*``          any run of characters except ``/``
  - ``**``         any run of characters, including ``/``
  - trailing ``/`` a directory marker, equivalent to "<pattern>/**"
"""

from __future__ import annotations

import re


def normalize(pattern: str) -> str:
    p = pattern.strip().lstrip("/")
    if p.endswith("/"):
        p += "**"
    return p


def to_pathspec(pattern: str) -> str:
    """A gitignore-style pattern -> one git pathspec magic token, for the
    exclude side of a ``format-patch``/``diff`` pathspec argument list."""
    p = normalize(pattern)
    if "/" not in p:
        return f":(exclude,glob)**/{p}"
    return f":(exclude,glob,top){p}"


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """A gitignore-style pattern -> a compiled regex matching a repo-relative,
    forward-slash path (as ``git diff --name-only`` reports it)."""
    p = normalize(pattern)
    if "/" not in p:
        p = f"**/{p}"

    segments = p.split("/")
    parts: list[str] = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            parts.append(".*" if last else "(?:.*/)?")
        else:
            parts.append(_translate_segment(segment) + ("" if last else "/"))
    return re.compile("^" + "".join(parts) + "$")


def _translate_segment(glob: str) -> str:
    out = []
    for ch in glob:
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
    return "".join(out)


def matches_any(path: str, patterns: list[str]) -> str | None:
    """The first pattern in ``patterns`` that matches ``path``, or ``None``."""
    for pattern in patterns:
        if compile_pattern(pattern).match(path):
            return pattern
    return None


def dedupe(patterns: list[str]) -> list[str]:
    seen: list[str] = []
    for pattern in patterns:
        pattern = pattern.strip()
        if pattern and pattern not in seen:
            seen.append(pattern)
    return seen
