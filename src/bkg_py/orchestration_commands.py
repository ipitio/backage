"""CLI adapters for Python-owned application orchestration decisions."""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

from .orchestration import BatchRuntimeService
from .result import ExitStatus
from .state import StateValueError

if TYPE_CHECKING:
    from .application import ApplicationContext


def run_orchestration(
    args: argparse.Namespace,
    application: ApplicationContext,
) -> ExitStatus:
    """Run one orchestration operation against the shared application state."""

    try:
        if args.orchestration_command != "begin-run":
            sys.stderr.write(
                f"unknown orchestration command: {args.orchestration_command}\n"
            )
            return ExitStatus.NON_FATAL
        application.ensure_state_file()
        result = BatchRuntimeService(application.state).begin_run(
            args.today,
            args.started_at,
        )
    except (OSError, StateValueError, ValueError) as error:
        sys.stderr.write(f"{error}\n")
        return ExitStatus.NON_FATAL
    sys.stdout.write(f"{result.batch_first_started}\n")
    return ExitStatus.SUCCESS
