"""Tests for adaptive remote-request backpressure."""

from __future__ import annotations

from bkg_py.enrichment import RequestCircuit, RequestCircuitSettings

_VERSION_SCOPE = "version"


def test_circuit_recovers_through_one_half_open_probe() -> None:
    """Repeated failures pause work, then one successful probe restores traffic."""

    now = 0.0
    circuit = RequestCircuit(
        RequestCircuitSettings(
            max_concurrent=2,
            failure_threshold=2,
            cooldown_seconds=5,
            max_cooldown_seconds=20,
        ),
        clock=lambda: now,
    )

    with circuit.request(_VERSION_SCOPE) as lease:
        assert lease
        assert lease.record_transient_failure() is None
    with circuit.request(_VERSION_SCOPE) as lease:
        assert lease
        assert lease.record_transient_failure() == 5
    with circuit.request(_VERSION_SCOPE) as enabled:
        assert not enabled

    now = 5
    with circuit.request(_VERSION_SCOPE) as probe:
        assert probe
        with circuit.request(_VERSION_SCOPE) as competing_probe:
            assert not competing_probe
        probe.record_success()

    with circuit.request(_VERSION_SCOPE) as enabled:
        assert enabled


def test_failed_probe_increases_cooldown_to_the_configured_limit() -> None:
    """Half-open failures back off without permanently disabling enrichment."""

    now = 0.0
    circuit = RequestCircuit(
        RequestCircuitSettings(
            max_concurrent=1,
            failure_threshold=1,
            cooldown_seconds=5,
            max_cooldown_seconds=10,
        ),
        clock=lambda: now,
    )

    with circuit.request(_VERSION_SCOPE) as lease:
        assert lease
        assert lease.record_transient_failure() == 5
    now = 5
    with circuit.request(_VERSION_SCOPE) as probe:
        assert probe
        assert probe.record_transient_failure() == 10
    now = 14
    with circuit.request(_VERSION_SCOPE) as enabled:
        assert not enabled
    now = 15
    with circuit.request(_VERSION_SCOPE) as probe:
        assert probe
        assert probe.record_transient_failure() == 10


def test_success_only_resets_its_own_metric_scope() -> None:
    """Healthy package pages cannot hide repeated version-page failures."""

    circuit = RequestCircuit(
        RequestCircuitSettings(failure_threshold=2, cooldown_seconds=5)
    )

    with circuit.request("version") as version:
        assert version.record_transient_failure() is None
    with circuit.request("package") as package:
        package.record_success()
    with circuit.request("version") as version:
        assert version.record_transient_failure() == 5
    with circuit.request("package") as enabled:
        assert enabled
    with circuit.request("version") as enabled:
        assert not enabled


def test_stale_success_cannot_close_a_newer_cooldown() -> None:
    """An older in-flight success cannot undo failures from its generation."""

    circuit = RequestCircuit(
        RequestCircuitSettings(
            max_concurrent=3,
            failure_threshold=2,
            cooldown_seconds=5,
        )
    )

    with (
        circuit.request(_VERSION_SCOPE) as first,
        circuit.request(_VERSION_SCOPE) as second,
        circuit.request(_VERSION_SCOPE) as stale,
    ):
        assert first.record_transient_failure() is None
        assert second.record_transient_failure() == 5
        stale.record_success()

    with circuit.request(_VERSION_SCOPE) as enabled:
        assert not enabled
