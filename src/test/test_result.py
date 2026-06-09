"""Tests for process outcomes shared by bkg commands."""

import unittest

from bkg_py.result import ExitStatus


class ExitStatusTests(unittest.TestCase):
    """Verify the externally meaningful process status values."""

    def test_status_values_match_existing_entrypoints(self) -> None:
        """Each named outcome retains its established numeric status."""

        self.assertEqual(int(ExitStatus.SUCCESS), 0)
        self.assertEqual(int(ExitStatus.NON_FATAL), 1)
        self.assertEqual(int(ExitStatus.FAILURE), 2)
        self.assertEqual(int(ExitStatus.GRACEFUL_STOP), 3)

    def test_statuses_are_valid_process_codes(self) -> None:
        """Statuses can be passed directly to SystemExit."""

        for status in ExitStatus:
            with self.subTest(status=status):
                self.assertIsInstance(status, int)


if __name__ == "__main__":
    unittest.main()
