"""Adaptive backpressure for optional GitHub metadata enrichment."""

from __future__ import annotations

import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import BoundedSemaphore, Lock

from .github import (
    GitHubError,
    GitHubResponseError,
    GitHubTextRequestPolicy,
    GitHubTransportError,
)

StopCheck = Callable[[], None]
Clock = Callable[[], float]

METRIC_TEXT_REQUEST_POLICY = GitHubTextRequestPolicy(
    total_timeout=30.0,
    max_attempts=1,
)
_GATE_POLL_SECONDS = 0.1
_TRANSIENT_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
PACKAGE_METRIC_SCOPE = "package"
VERSION_METRIC_SCOPE = "version"


def _ignore_stop() -> None:
    pass


@dataclass(frozen=True)
class MetricEnrichmentSettings:
    """Concurrency and recovery limits for optional GitHub metric pages."""

    max_concurrent: int = 2
    failure_threshold: int = 2
    cooldown_seconds: float = 300.0
    max_cooldown_seconds: float = 1800.0

    def __post_init__(self) -> None:
        if self.max_concurrent < 1:
            raise ValueError("metric enrichment concurrency must be positive")
        if self.failure_threshold < 1:
            raise ValueError("metric enrichment failure threshold must be positive")
        if self.cooldown_seconds <= 0:
            raise ValueError("metric enrichment cooldown must be positive")
        if self.max_cooldown_seconds < self.cooldown_seconds:
            raise ValueError(
                "metric enrichment maximum cooldown must not be shorter than cooldown"
            )


DEFAULT_METRIC_ENRICHMENT_SETTINGS = MetricEnrichmentSettings()


@dataclass
class _CircuitState:
    """Mutable recovery state isolated to one metric endpoint class."""

    cooldown: float
    consecutive_failures: int = 0
    open_until: float | None = None
    probe_in_flight: bool = False
    generation: int = 0


@dataclass(frozen=True)
class MetricEnrichmentLease:
    """One admitted metric request bound to its circuit-state generation."""

    _success_recorder: Callable[[MetricEnrichmentLease], None] = field(
        repr=False,
        compare=False,
    )
    _failure_recorder: Callable[[MetricEnrichmentLease], float | None] = field(
        repr=False,
        compare=False,
    )
    scope: str
    generation: int
    enabled: bool
    probe: bool = False

    def __bool__(self) -> bool:
        return self.enabled

    def record_success(self) -> None:
        """Close this request's circuit state when its generation is current."""

        self._success_recorder(self)

    def record_transient_failure(self) -> float | None:
        """Record a recoverable failure for this request's generation."""

        return self._failure_recorder(self)


class MetricEnrichmentCircuit:  # pylint: disable=too-few-public-methods
    """Bound metric-page traffic and periodically probe after transient failures."""

    def __init__(
        self,
        settings: MetricEnrichmentSettings = DEFAULT_METRIC_ENRICHMENT_SETTINGS,
        *,
        check_stop: StopCheck = _ignore_stop,
        clock: Clock = time.monotonic,
    ) -> None:
        self._semaphore = BoundedSemaphore(settings.max_concurrent)
        self._lock = Lock()
        self._settings = settings
        self._check_stop = check_stop
        self._clock = clock
        self._states: dict[str, _CircuitState] = {}

    @contextmanager
    def request(self, scope: str) -> Generator[MetricEnrichmentLease, None, None]:
        """Yield one generation-bound normal request or half-open probe lease."""

        if not scope:
            raise ValueError("metric enrichment scope is required")
        while not self._semaphore.acquire(timeout=_GATE_POLL_SECONDS):
            self._check_stop()
        lease: MetricEnrichmentLease | None = None
        try:
            self._check_stop()
            with self._lock:
                now = self._clock()
                state = self._state(scope)
                if state.open_until is None:
                    enabled = True
                    probe = False
                elif now < state.open_until or state.probe_in_flight:
                    enabled = False
                    probe = False
                else:
                    enabled = True
                    probe = True
                    state.probe_in_flight = True
                lease = MetricEnrichmentLease(
                    self._record_success,
                    self._record_transient_failure,
                    scope,
                    state.generation,
                    enabled,
                    probe,
                )
            yield lease
        finally:
            if lease is not None and lease.probe:
                with self._lock:
                    state = self._state(scope)
                    if state.generation == lease.generation and state.probe_in_flight:
                        state.probe_in_flight = False
                        state.open_until = self._clock() + state.cooldown
                        state.generation += 1
            self._semaphore.release()

    def _record_success(self, lease: MetricEnrichmentLease) -> None:
        """Close the circuit after a current-generation usable response."""

        with self._lock:
            state = self._state(lease.scope)
            if not lease.enabled or state.generation != lease.generation:
                return
            was_open = state.open_until is not None
            state.consecutive_failures = 0
            state.cooldown = self._settings.cooldown_seconds
            state.open_until = None
            state.probe_in_flight = False
            if was_open:
                state.generation += 1

    def _record_transient_failure(
        self,
        lease: MetricEnrichmentLease,
    ) -> float | None:
        """Record a current-generation failure and return a new cooldown."""

        with self._lock:
            state = self._state(lease.scope)
            if not lease.enabled or state.generation != lease.generation:
                return None
            if state.open_until is not None:
                if not lease.probe or not state.probe_in_flight:
                    return None
                state.probe_in_flight = False
                state.cooldown = min(
                    state.cooldown * 2,
                    self._settings.max_cooldown_seconds,
                )
                state.open_until = self._clock() + state.cooldown
                state.generation += 1
                return state.cooldown

            state.consecutive_failures += 1
            if state.consecutive_failures < self._settings.failure_threshold:
                return None
            state.consecutive_failures = 0
            state.cooldown = self._settings.cooldown_seconds
            state.open_until = self._clock() + state.cooldown
            state.generation += 1
            return state.cooldown

    def _state(self, scope: str) -> _CircuitState:
        return self._states.setdefault(
            scope,
            _CircuitState(self._settings.cooldown_seconds),
        )


def transient_enrichment_error(error: GitHubError) -> bool:
    """Return whether optional enrichment may recover after a cooldown."""

    return isinstance(error, GitHubTransportError) or (
        isinstance(error, GitHubResponseError)
        and error.status_code in _TRANSIENT_STATUS_CODES
    )
