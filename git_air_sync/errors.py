"""Exception hierarchy and process exit codes, in one place.

Exit codes are part of the tool's contract (scripts and the test-suite assert on
them), so they live here rather than being scattered through ``main.py``.
"""


class AirSyncError(Exception):
    """Base class for every error this tool raises deliberately."""

    exit_code = 1


class UserAbort(AirSyncError):
    """The user chose to stop, or there was nothing to do."""

    exit_code = 1


class NothingToDo(UserAbort):
    """Already in sync — not a failure."""

    exit_code = 0


class MergeConflict(AirSyncError):
    """A merge stopped with conflicts that need a human."""

    exit_code = 2


class PayloadError(AirSyncError):
    """The .docx could not be decoded, unwrapped, or verified."""

    exit_code = 3


class EnvironmentError_(AirSyncError):
    """Missing git, bad config, wrong machine role, no disk space."""

    exit_code = 4


class NonInteractiveError(EnvironmentError_):
    """A prompt was needed but stdin is not a TTY.

    Carries the name of the flag that would have supplied the value, so the
    message can tell the caller how to script around it.
    """

    def __init__(self, what: str, flag: str | None = None) -> None:
        msg = f"{what} is required but there is no terminal to ask on"
        if flag:
            msg += f"; pass {flag}"
        super().__init__(msg)
        self.flag = flag


EXIT_INTERRUPTED = 130
