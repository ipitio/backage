"""Shared GitHub REST capacity and API usage accounting."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from typing import cast

from ..runtime_names import StateKey
from ..state import StateStore
from .settings import DEFAULT_REST_RESERVE

_RATE_RESET_BUFFER_SECONDS = 1.0
_ACCOUNTING_FLUSH_RESPONSES = 32


@dataclass(frozen=True)
class GitHubRateWait:
    """A required pause before another authenticated REST request."""

    seconds: float | None
    message: str
    report: bool


@dataclass
class _GitHubRateWindow:
    in_flight: int = 0
    reported_reset_at: int | None = None
    remaining: int | None = None
    reset_at: int | None = None


def _empty_rate_values() -> dict[str, str | int]:
    return {}


@dataclass
class _GitHubPendingUsage:
    values: dict[str, str | int] = field(default_factory=_empty_rate_values)
    calls: int = 0
    minute_calls: int = 0
    responses: int = 0


class GitHubRateAccounting:
    """Share and persist REST capacity plus REST and GraphQL usage."""

    def __init__(
        self,
        state: StateStore,
        *,
        rest_reserve: int = DEFAULT_REST_RESERVE,
        flush_responses: int = _ACCOUNTING_FLUSH_RESPONSES,
    ) -> None:
        if rest_reserve < 0:
            raise ValueError("REST reserve must be zero or greater")
        if flush_responses < 1:
            raise ValueError("accounting flush responses must be positive")
        self.state = state
        self.rest_reserve = rest_reserve
        self.flush_responses = flush_responses
        self._lock = Lock()
        self._rate = _GitHubRateWindow(
            remaining=_nonnegative_int(state.get(StateKey.REST_REMAINING)),
            reset_at=_nonnegative_int(state.get(StateKey.REST_RESET_AT)),
        )
        self._pending = _GitHubPendingUsage()

    def reserve_rest(self, now: float) -> GitHubRateWait | None:
        """Reserve one request or describe how long capacity must wait."""

        with self._lock:
            if self._rate.reset_at is not None and self._rate.reset_at <= now:
                self._rate.remaining = None
                self._rate.reset_at = None
                self._rate.reported_reset_at = None

            available = (
                None
                if self._rate.remaining is None
                else self._rate.remaining - self._rate.in_flight
            )
            if available is None or available > self.rest_reserve:
                self._rate.in_flight += 1
                return None

            reset_at = self._rate.reset_at
            report = reset_at != self._rate.reported_reset_at
            self._rate.reported_reset_at = reset_at

        reserve = self.rest_reserve
        if reset_at is None:
            return GitHubRateWait(
                None,
                f"GitHub REST budget reached its {reserve}-request workflow "
                "reserve, but GitHub did not report a reset time",
                report,
            )
        reset_time = datetime.fromtimestamp(reset_at, UTC).isoformat()
        seconds = max(0.0, reset_at - now + _RATE_RESET_BUFFER_SECONDS)
        return GitHubRateWait(
            seconds,
            f"GitHub REST budget reached its {reserve}-request workflow reserve; "
            f"waiting {seconds:.0f}s for reset at {reset_time}",
            report,
        )

    def record_rest(
        self,
        headers: Mapping[str, str],
        *,
        budgeted: bool = True,
    ) -> None:
        """Count one REST response and retain its latest rate-limit headers."""

        values = self._complete_rest_request(headers) if budgeted else {}
        self._record_usage(values, calls=1, minute_calls=1)

    def cancel_rest(self) -> None:
        """Release a reservation when no REST response was received."""

        with self._lock:
            self._rate.in_flight = max(0, self._rate.in_flight - 1)

    def record_graphql(self, value: object) -> None:
        """Count one GraphQL response using GitHub's reported query cost."""

        rate_limit = _graphql_rate_limit(value)
        cost = _positive_int(rate_limit.get("cost"), default=1)
        values: dict[str, str | int] = {StateKey.GRAPHQL_LAST_COST: cost}
        remaining = _nonnegative_int(rate_limit.get("remaining"))
        if remaining is not None:
            values[StateKey.GRAPHQL_REMAINING] = remaining
        reset_at = rate_limit.get("resetAt")
        if isinstance(reset_at, str) and reset_at:
            values[StateKey.GRAPHQL_RESET_AT] = reset_at
        self._record_usage(values, calls=cost, minute_calls=cost)

    def flush(self) -> None:
        """Persist accumulated response accounting in one state replacement."""

        with self._lock:
            pending = self._pending
            self._pending = _GitHubPendingUsage()
        if not pending.values and pending.calls == 0 and pending.minute_calls == 0:
            return
        try:
            self.state.update_many(
                pending.values,
                increments={
                    StateKey.CALLS_TO_API: pending.calls,
                    StateKey.MIN_CALLS_TO_API: pending.minute_calls,
                },
            )
        except BaseException:
            with self._lock:
                current = self._pending
                pending.values.update(current.values)
                pending.calls += current.calls
                pending.minute_calls += current.minute_calls
                pending.responses += current.responses
                self._pending = pending
            raise

    def _record_usage(
        self,
        values: Mapping[str, str | int],
        *,
        calls: int,
        minute_calls: int,
    ) -> None:
        should_flush = False
        with self._lock:
            self._pending.values.update(values)
            self._pending.calls += calls
            self._pending.minute_calls += minute_calls
            self._pending.responses += 1
            should_flush = self._pending.responses >= self.flush_responses
        if should_flush:
            self.flush()

    def _complete_rest_request(
        self,
        headers: Mapping[str, str],
    ) -> dict[str, int]:
        remaining = _nonnegative_int(headers.get("x-ratelimit-remaining"))
        reset_at = _nonnegative_int(headers.get("x-ratelimit-reset"))
        limit = _nonnegative_int(headers.get("x-ratelimit-limit"))

        with self._lock:
            self._rate.in_flight = max(0, self._rate.in_flight - 1)
            if (
                reset_at is not None
                and self._rate.reset_at is not None
                and reset_at < self._rate.reset_at
            ):
                remaining = None
                reset_at = None
            elif reset_at is not None and reset_at != self._rate.reset_at:
                self._rate.reset_at = reset_at
                self._rate.remaining = remaining
                self._rate.reported_reset_at = None
            elif remaining is not None:
                self._rate.remaining = (
                    remaining
                    if self._rate.remaining is None
                    else min(self._rate.remaining, remaining)
                )

            values: dict[str, int] = {}
            if self._rate.remaining is not None:
                values[StateKey.REST_REMAINING] = self._rate.remaining
            if self._rate.reset_at is not None:
                values[StateKey.REST_RESET_AT] = self._rate.reset_at
            if limit is not None:
                values[StateKey.REST_LIMIT] = limit
            return values


def _graphql_rate_limit(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        return {}
    data = cast(dict[str, object], value).get("data")
    if not isinstance(data, dict):
        return {}
    rate_limit = cast(dict[str, object], data).get("rateLimit")
    return cast(dict[str, object], rate_limit) if isinstance(rate_limit, dict) else {}


def _positive_int(value: object, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return default
    return parsed if parsed > 0 else default


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return None
    return parsed if parsed >= 0 else None
