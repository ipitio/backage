"""Process outcomes shared by bkg commands and long-running operations."""

from enum import IntEnum, unique


@unique
class ExitStatus(IntEnum):
    """Process statuses that have defined meaning across bkg entrypoints."""

    SUCCESS = 0
    NON_FATAL = 1
    FAILURE = 2
    GRACEFUL_STOP = 3


PUBLIC_EXIT_STATUSES = frozenset(
    {
        ExitStatus.SUCCESS,
        ExitStatus.NON_FATAL,
        ExitStatus.GRACEFUL_STOP,
    }
)
