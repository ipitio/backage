"""Tests for the shell-compatible persisted state store."""

import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from bkg_py.state import StateStore, StateValueError

_REPO_ROOT = Path(__file__).resolve().parents[2]
_UTIL_SH = _REPO_ROOT / "src" / "lib" / "util.sh"


class TestStateStore:
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

            assert store.get("KNOWN") == "new"
            assert store.get("UNKNOWN") == "keep"
            text = path.read_text(encoding="utf-8")
            assert "# retained\n" in text
            assert "unrecognized line\n" in text
            assert text.endswith("KNOWN=new\n\n")

    def test_set_operations_are_ordered_and_unique(self) -> None:
        """Newline-backed sets retain insertion order and reject duplicates."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env.env"
            path.touch()
            store = StateStore(path)

            assert store.add_to_set("BKG_QUEUE", "alpha")
            assert store.add_to_set("BKG_QUEUE", "beta")
            assert not store.add_to_set("BKG_QUEUE", "alpha")

            assert store.get_set("BKG_QUEUE") == ["alpha", "beta"]
            assert r"BKG_QUEUE=alpha\nbeta" in path.read_text(encoding="utf-8")

    def test_bulk_set_operations_use_one_ordered_transition(self) -> None:
        """Bulk replacement and addition retain order without duplicate entries."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env.env"
            path.touch()
            store = StateStore(path)

            assert store.replace_set("BKG_QUEUE", ("alpha", "beta", "alpha")) == (
                "alpha",
                "beta",
            )
            assert store.add_many_to_set(
                "BKG_QUEUE",
                ("beta", "gamma", "delta", "gamma"),
            ) == ("gamma", "delta")

            assert store.get_set("BKG_QUEUE") == [
                "alpha",
                "beta",
                "gamma",
                "delta",
            ]

    def test_concurrent_updates_do_not_lose_unrelated_values(self) -> None:
        """The shared hard-link lock serializes complete file replacements."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env.env"
            path.touch()
            store = StateStore(path, lock_poll_interval=0.001)

            def set_value(number: int) -> None:
                store.set(f"BKG_VALUE_{number}", number)

            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(set_value, range(32)))

            snapshot = store.snapshot()
            for number in range(32):
                assert snapshot[f"BKG_VALUE_{number}"] == str(number)

    def test_increment_is_atomic_across_threads(self) -> None:
        """Counters use one read-modify-write operation under the global lock."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env.env"
            path.touch()
            store = StateStore(path, lock_poll_interval=0.001)

            def increment(_: int) -> int:
                return store.increment("BKG_COUNT")

            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(increment, range(40)))

            assert store.get_int("BKG_COUNT") == 40

    def test_values_and_counters_update_in_one_replacement(self) -> None:
        """Accounting can persist headers and counters as one state change."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env.env"
            path.write_text("BKG_COUNT=4\nUNKNOWN=keep\n\n", encoding="utf-8")
            store = StateStore(path)

            counters = store.update_many(
                {"BKG_REMAINING": 50},
                increments={"BKG_COUNT": 3, "BKG_OTHER_COUNT": 2},
            )

            assert counters == {"BKG_COUNT": 7, "BKG_OTHER_COUNT": 2}
            assert store.snapshot() == {
                "UNKNOWN": "keep",
                "BKG_REMAINING": "50",
                "BKG_COUNT": "7",
                "BKG_OTHER_COUNT": "2",
            }

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

            assert deleted == {
                "BKG_PACKAGES_alpha",
                "BKG_VERSIONS_alpha",
                "BKG_OWNERS_QUEUE",
                "BKG_TIMEOUT",
            }
            assert store.snapshot() == {"BKG_BATCH_MARKER": "durable"}

    def test_failed_replace_keeps_original_and_cleans_temporary_file(self) -> None:
        """A failed atomic replacement leaves the previous state readable."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env.env"
            path.write_text("BKG_VALUE=old\n\n", encoding="utf-8")
            store = StateStore(path)

            with (
                patch.object(
                    Path,
                    "replace",
                    side_effect=OSError("replace failed"),
                ),
                pytest.raises(OSError, match="replace failed"),
            ):
                store.set("BKG_VALUE", "new")

            assert path.read_text(encoding="utf-8") == "BKG_VALUE=old\n\n"
            assert not list(path.parent.glob(f".{path.name}.*"))
            assert not Path(f"{path}.lock").exists()

    def test_unrepresentable_values_are_rejected(self) -> None:
        """Values that Bash would split or truncate cannot be persisted."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env.env"
            path.touch()
            store = StateStore(path)

            with pytest.raises(StateValueError):
                store.set("BKG_VALUE", "line one\nline two")
            with pytest.raises(StateValueError):
                store.set("BKG_VALUE", "left=right")
            with pytest.raises(StateValueError):
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
            assert store.get("BKG_SCALAR") == "from-bash"
            assert store.get_set("BKG_QUEUE") == ["alpha", "beta"]
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
            assert result.stdout == "from-python\nalpha\nbeta\ngamma\nunknown=\n"
