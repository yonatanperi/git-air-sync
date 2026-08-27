"""Interactive prompts, with a stdlib fallback for machines without questionary.

Every wrapper refuses rather than blocks when stdin is not a terminal: a prompt that
silently hangs inside a script is the worst failure mode this tool could have.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from ..errors import NonInteractiveError, UserAbort
from . import theme
from .theme import icon


@dataclass(frozen=True)
class Choice:
    value: Any
    label: str
    description: str = ""
    is_default: bool = False


def _require_tty(what: str, flag: str | None) -> None:
    if not sys.stdin.isatty():
        raise NonInteractiveError(what, flag)


def _ask(prompt: str) -> str:
    """``input()`` that treats a closed stdin as "the user gave up", not a crash."""
    try:
        return input(prompt)
    except EOFError:
        raise UserAbort("Cancelled — input ended.") from None


def _questionary_style() -> Any:
    from questionary import Style

    return Style(
        [
            ("qmark", "fg:#00d7ff bold"),
            ("question", "bold"),
            ("answer", "fg:#ff87ff bold"),
            ("pointer", "fg:#00d7ff bold"),
            ("highlighted", "fg:#00d7ff bold"),
            ("selected", "fg:#5fff87"),
            ("instruction", "fg:#808080"),
        ]
    )


def select(
    message: str,
    choices: Sequence[Choice],
    *,
    default: Any = None,
    flag: str | None = None,
) -> Any:
    """A searchable single-select list. ``*`` marks the default."""
    if not choices:
        raise UserAbort("Nothing to choose from.")
    if len(choices) == 1:
        return choices[0].value

    _require_tty(message, flag)

    if theme.HAS_QUESTIONARY:
        import questionary

        mapping = {}
        options = []
        for choice in choices:
            label = choice.label + (" *" if choice.is_default else "")
            if choice.description:
                label = f"{label}  —  {choice.description}"
            mapping[label] = choice.value
            options.append(label)

        default_label = next(
            (lbl for lbl, val in mapping.items() if val == default), None
        )
        answer = questionary.select(
            message,
            choices=options,
            default=default_label,
            style=_questionary_style(),
            qmark=icon("bullet"),
            use_search_filter=True,
            use_jk_keys=False,  # would otherwise type into the search box
            instruction="(type to filter, arrows to move, enter to pick)",
        ).ask()
        if answer is None:
            raise UserAbort("Cancelled.")
        return mapping[answer]

    return _plain_select(message, choices, default)


def _plain_select(message: str, choices: Sequence[Choice], default: Any) -> Any:
    """Numbered list with a substring filter — poor man's fuzzy search."""
    pool = list(choices)
    while True:
        print(f"\n{message}")
        for index, choice in enumerate(pool, start=1):
            marker = " *" if choice.is_default or choice.value == default else ""
            suffix = f"  ({choice.description})" if choice.description else ""
            print(f"  {index:>2}. {choice.label}{marker}{suffix}")

        default_index = next(
            (i for i, c in enumerate(pool, 1) if c.value == default), None
        )
        hint = f" [{default_index}]" if default_index else ""
        raw = _ask(f"Choose a number, or type text to filter{hint}: ").strip()

        if not raw and default_index:
            return pool[default_index - 1].value
        if raw.isdigit():
            number = int(raw)
            if 1 <= number <= len(pool):
                return pool[number - 1].value
            print(f"  {icon('err')} Enter a number between 1 and {len(pool)}.")
            continue

        matches = [c for c in pool if raw.lower() in c.label.lower()]
        if not matches:
            print(f"  {icon('err')} Nothing matches {raw!r}.")
        elif len(matches) == 1:
            return matches[0].value
        else:
            pool = matches


def confirm(message: str, *, default: bool = True, flag: str | None = None) -> bool:
    _require_tty(message, flag)

    if theme.HAS_QUESTIONARY:
        import questionary

        answer = questionary.confirm(
            message,
            default=default,
            style=_questionary_style(),
            qmark=icon("bullet"),
            auto_enter=True,  # single keypress, per the spec
        ).ask()
        if answer is None:
            raise UserAbort("Cancelled.")
        return bool(answer)

    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        raw = _ask(f"{message} {suffix} ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print(f"  {icon('err')} Please answer y or n.")


def text(
    message: str,
    *,
    default: str | None = None,
    validate: Callable[[str], tuple[bool, str]] | None = None,
    flag: str | None = None,
) -> str:
    _require_tty(message, flag)

    while True:
        if theme.HAS_QUESTIONARY:
            import questionary

            answer = questionary.text(
                message,
                default=default or "",
                style=_questionary_style(),
                qmark=icon("bullet"),
            ).ask()
            if answer is None:
                raise UserAbort("Cancelled.")
        else:
            hint = f" [{default}]" if default else ""
            answer = _ask(f"{message}{hint}: ").strip() or (default or "")

        if validate:
            ok, reason = validate(answer)
            if not ok:
                print(f"  {icon('err')} {reason}")
                continue
        return answer


def path_prompt(
    message: str,
    *,
    default: str | None = None,
    validate: Callable[[str], tuple[bool, str]] | None = None,
    flag: str | None = None,
) -> Path:
    _require_tty(message, flag)

    if theme.HAS_QUESTIONARY:
        import questionary

        while True:
            answer = questionary.path(
                message,
                default=default or "",
                style=_questionary_style(),
                qmark=icon("bullet"),
            ).ask()
            if answer is None:
                raise UserAbort("Cancelled.")
            if validate:
                ok, reason = validate(answer)
                if not ok:
                    print(f"  {icon('err')} {reason}")
                    continue
            return Path(answer).expanduser()

    return Path(text(message, default=default, validate=validate)).expanduser()


def press_enter(message: str = "Press Enter to continue") -> None:
    if sys.stdin.isatty():
        _ask(f"{message}… ")
