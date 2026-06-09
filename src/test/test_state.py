"""Tests for the shell-compatible persisted state store."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from bkg_py.state import StateStore, StateValueError

_REPO_ROOT = Path(__file__).resolve().parents[2]
_UTIL_SH = _REPO_ROOT / "src" / "lib" / "util.sh"


class StateStoreTests(unittest.TestCase):
    """Exercise state compatibility, locking, and atomic file replacement."""

    def test_scalar_updates_preserve_unknown_records(self) -> None:
        """Known updates do not discard keys or lines owned by newer versions."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env.env"
            path.write_text(
                "# retained\nUNKNOWN=keep\nunrecognized line\nKNOWN=old\n\n",
                encoding="utf-8",
            )
            store = StateStore(path)

            store.set("KNOWN", "new")

            self.assertEqual(store.get("KNOWN"), "new")
            self.assertEqual(store.get("UNKNOWN"), "keep")
            text = path.read_text(encoding="utf-8")
            self.assertIn("# retained\n", text)
            self.assertIn("unrecognized line\n", text)
            self.assertTrue(text.endswith("KNOWN=new\n\n"))

    def test_set_operations_are_ordered_and_unique(self) -> None:
        """Newline-backed sets retain insertion order and reject duplicates."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env.env"
            path.touch()
            store = StateStore(path)

            self.assertTrue(store.add_to_set("BKG_QUEUE", "alpha"))
            self.assertTrue(store.add_to_set("BKG_QUEUE", "beta"))
            self.assertFalse(store.add_to_set("BKG_QUEUE", "alpha"))

            self.assertEqual(store.get_set("BKG_QUEUE"), ["alpha", "beta"])
            self.assertIn(
                r"BKG_QUEUE=alpha\nbeta",
                path.read_text(encoding="utf-8"),
            )

    def test_concurrent_updates_do_not_lose_unrelated_values(self) -> None:
        """The shared hard-link lock serializes complete file replacements."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env.env"
            path.touch()
            store = StateStore(path, lock_poll_interval=0.001)

            with ThreadPoolExecutor(max_workers=8) as executor:
                list(
                    executor.map(
                        lambda number: store.set(f"BKG_VALUE_{number}", number),
                        range(32),
                    )
                )

            snapshot = store.snapshot()
            for number in range(32):
                self.assertEqual(snapshot[f"BKG_VALUE_{number}"], str(number))

    def test_increment_is_atomic_across_threads(self) -> None:
        """Counters use one read-modify-write operation under the global lock."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env.env"
            path.touch()
            store = StateStore(path, lock_poll_interval=0.001)

            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(lambda _: store.increment("BKG_COUNT"), range(40)))

            self.assertEqual(store.get_int("BKG_COUNT"), 40)

    def test_delete_matching_supports_final_state_pruning(self) -> None:
        """Transient families can be removed without dropping durable keys."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env.env"
            path.touch()
            store = StateStore(path)
            store.set_many(
                {
                    "BKG_PACKAGES_alpha": r"one\ntwo",
                    "BKG_VERSIONS_alpha": "one",
                    "BKG_OWNERS_QUEUE": "one",
                    "BKG_TIMEOUT": "1",
                    "BKG_BATCH_MARKER": "durable",
                }
            )

            deleted = store.delete_matching(
                keys=("BKG_TIMEOUT",),
                prefixes=("BKG_PACKAGES_", "BKG_VERSIONS_", "BKG_OWNERS_"),
            )

            self.assertEqual(
                deleted,
                {
                    "BKG_PACKAGES_alpha",
                    "BKG_VERSIONS_alpha",
                    "BKG_OWNERS_QUEUE",
                    "BKG_TIMEOUT",
                },
            )
            self.assertEqual(store.snapshot(), {"BKG_BATCH_MARKER": "durable"})

    def test_failed_replace_keeps_original_and_cleans_temporary_file(self) -> None:
        """A failed atomic replacement leaves the previous state readable."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env.env"
            path.write_text("BKG_VALUE=old\n\n", encoding="utf-8")
            store = StateStore(path)

            with mock.patch("bkg_py.state.os.replace", side_effect=OSError):
                with self.assertRaises(OSError):
                    store.set("BKG_VALUE", "new")

            self.assertEqual(path.read_text(encoding="utf-8"), "BKG_VALUE=old\n\n")
            self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])
            self.assertFalse(Path(f"{path}.lock").exists())

    def test_unrepresentable_values_are_rejected(self) -> None:
        """Values that Bash would split or truncate cannot be persisted."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env.env"
            path.touch()
            store = StateStore(path)

            with self.assertRaises(StateValueError):
                store.set("BKG_VALUE", "line one\nline two")
            with self.assertRaises(StateValueError):
                store.set("BKG_VALUE", "left=right")
            with self.assertRaises(StateValueError):
                store.set("not-a-shell-name", "value")

    def test_bash_and_python_read_each_others_updates(self) -> None:
        """Both implementations share scalar and newline-backed set state."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env.env"
            path.touch()
            bash_write = r"""
set -uo pipefail
export BKG_SKIP_DEP_VERIFY=1
export BKG_ENV=$1
source "$2"
set_BKG BKG_SCALAR from-bash
set_BKG BKG_UNKNOWN preserved
set_BKG_set BKG_QUEUE alpha
set_BKG_set BKG_QUEUE beta
set_BKG_set BKG_QUEUE alpha || :
"""
            subprocess.run(
                ["bash", "-c", bash_write, "bash", str(path), str(_UTIL_SH)],
                check=True,
            )

            store = StateStore(path)
            self.assertEqual(store.get("BKG_SCALAR"), "from-bash")
            self.assertEqual(store.get_set("BKG_QUEUE"), ["alpha", "beta"])
            store.set("BKG_SCALAR", "from-python")
            store.add_to_set("BKG_QUEUE", "gamma")
            store.delete("BKG_UNKNOWN")

            bash_read = r"""
set -euo pipefail
export BKG_SKIP_DEP_VERIFY=1
export BKG_ENV=$1
source "$2"
printf '%s\n' "$(get_BKG BKG_SCALAR)"
get_BKG_set BKG_QUEUE
printf 'unknown=%s\n' "$(get_BKG BKG_UNKNOWN)"
"""
            result = subprocess.run(
                ["bash", "-c", bash_read, "bash", str(path), str(_UTIL_SH)],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                result.stdout,
                "from-python\nalpha\nbeta\ngamma\nunknown=\n",
            )


if __name__ == "__main__":
    unittest.main()
