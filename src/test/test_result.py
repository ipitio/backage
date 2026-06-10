"""Tests for process outcomes shared by bkg commands."""

import pytest

from bkg_py.result import ExitStatus


class TestExitStatus:
    """Verify the externally meaningful process status values."""

    def test_status_values_match_existing_entrypoints(self) -> None:
        """Each named outcome retains its established numeric status."""

        assert int(ExitStatus.SUCCESS) == 0
        assert int(ExitStatus.NON_FATAL) == 1
        assert int(ExitStatus.FAILURE) == 2
        assert int(ExitStatus.GRACEFUL_STOP) == 3

    @pytest.mark.parametrize("status", list(ExitStatus))
    def test_statuses_are_valid_process_codes(self, status: ExitStatus) -> None:
        """Statuses can be passed directly to SystemExit."""

        assert isinstance(status, int)
