"""Tests for optional GitHub metric enrichment backpressure."""

from __future__ import annotations

from bkg_py.enrichment import MetricEnrichmentCircuit, MetricEnrichmentSettings

_VERSION_SCOPE = "version"


def test_circuit_recovers_through_one_half_open_probe() -> None:
    """Repeated failures pause work, then one successful probe restores traffic."""

    now = 0.0
    circuit = MetricEnrichmentCircuit(
        MetricEnrichmentSettings(
            max_concurrent=2,
            failure_threshold=2,
            cooldown_seconds=5,
            max_cooldown_seconds=20,
        ),
        clock=lambda: now,
    )

    with circuit.request(_VERSION_SCOPE) as enabled:
        assert enabled
        assert circuit.record_transient_failure(_VERSION_SCOPE) is None
    with circuit.request(_VERSION_SCOPE) as enabled:
        assert enabled
        assert circuit.record_transient_failure(_VERSION_SCOPE) == 5
    with circuit.request(_VERSION_SCOPE) as enabled:
        assert not enabled

    now = 5
    with circuit.request(_VERSION_SCOPE) as probe:
        assert probe
        with circuit.request(_VERSION_SCOPE) as competing_probe:
            assert not competing_probe
        circuit.record_success(_VERSION_SCOPE)

    with circuit.request(_VERSION_SCOPE) as enabled:
        assert enabled


def test_failed_probe_increases_cooldown_to_the_configured_limit() -> None:
    """Half-open failures back off without permanently disabling enrichment."""

    now = 0.0
    circuit = MetricEnrichmentCircuit(
        MetricEnrichmentSettings(
            max_concurrent=1,
            failure_threshold=1,
            cooldown_seconds=5,
            max_cooldown_seconds=10,
        ),
        clock=lambda: now,
    )

    with circuit.request(_VERSION_SCOPE) as enabled:
        assert enabled
        assert circuit.record_transient_failure(_VERSION_SCOPE) == 5
    now = 5
    with circuit.request(_VERSION_SCOPE) as probe:
        assert probe
        assert circuit.record_transient_failure(_VERSION_SCOPE) == 10
    now = 14
    with circuit.request(_VERSION_SCOPE) as enabled:
        assert not enabled
    now = 15
    with circuit.request(_VERSION_SCOPE) as probe:
        assert probe
        assert circuit.record_transient_failure(_VERSION_SCOPE) == 10


def test_success_only_resets_its_own_metric_scope() -> None:
    """Healthy package pages cannot hide repeated version-page failures."""

    circuit = MetricEnrichmentCircuit(
        MetricEnrichmentSettings(failure_threshold=2, cooldown_seconds=5)
    )

    with circuit.request("version"):
        assert circuit.record_transient_failure("version") is None
    with circuit.request("package"):
        circuit.record_success("package")
    with circuit.request("version"):
        assert circuit.record_transient_failure("version") == 5
    with circuit.request("package") as enabled:
        assert enabled
    with circuit.request("version") as enabled:
        assert not enabled
