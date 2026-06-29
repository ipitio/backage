"""GitHub HTTP operations with shared retries, accounting, and stop handling."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, cast

import httpx

from .files import atomic_binary_output
from .state import StateStore

_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
_FORBIDDEN_STATUS = 403
_NOT_FOUND_STATUS = 404
_ERROR_BODY_LIMIT = 500
_ERROR_BODY_PREFIX = 497
_SECONDARY_LIMIT_MARKERS = (
    "secondary rate limit",
    "abuse detection",
    "temporarily blocked",
)
_RATE_LIMIT_SELECTION = "rateLimit { cost remaining resetAt }"


class _HtmlTitleParser(HTMLParser):
    """Extract a compact title from an HTML error response."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_title = False
        self.parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del attrs
        if tag.casefold() == "title":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.parts.append(data)


class GitHubError(RuntimeError):
    """A GitHub HTTP operation could not complete."""


class GitHubTransportError(GitHubError):
    """GitHub could not be reached within the configured retry budget."""


class GitHubResponseError(GitHubError):
    """GitHub returned a non-success response."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubNotFoundError(GitHubResponseError):
    """GitHub reported that the requested resource does not exist."""


class GitHubGraphQLError(GitHubError):
    """GitHub returned one or more GraphQL errors."""


class GitHubDecodeError(GitHubError):
    """GitHub returned a response that was not valid JSON."""


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError as error:
        raise GitHubError(f"{name} must be a number") from error
    if parsed <= 0:
        raise GitHubError(f"{name} must be greater than zero")
    return parsed


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise GitHubError(f"{name} must be an integer") from error
    if parsed <= 0:
        raise GitHubError(f"{name} must be greater than zero")
    return parsed


@dataclass(frozen=True)
class GitHubSettings:  # pylint: disable=too-many-instance-attributes
    """Configuration for GitHub HTTP requests."""

    token: str = field(repr=False)
    api_url: str = "https://api.github.com"
    connect_timeout: float = 10.0
    read_timeout: float = 60.0
    write_timeout: float = 60.0
    pool_timeout: float = 10.0
    total_timeout: float = 120.0
    max_attempts: int = 5
    initial_backoff: float = 1.0
    max_backoff: float = 30.0
    user_agent: str = "backage"

    @classmethod
    def from_env(cls) -> GitHubSettings:
        """Load settings from the shell-compatible runtime environment."""

        return cls(
            token=os.environ.get("GITHUB_TOKEN", ""),
            api_url=os.environ.get("BKG_GITHUB_API_URL", "https://api.github.com"),
            connect_timeout=_env_float("BKG_HTTP_CONNECT_TIMEOUT", 10.0),
            read_timeout=_env_float("BKG_HTTP_READ_TIMEOUT", 60.0),
            write_timeout=_env_float("BKG_HTTP_WRITE_TIMEOUT", 60.0),
            pool_timeout=_env_float("BKG_HTTP_POOL_TIMEOUT", 10.0),
            total_timeout=_env_float("BKG_HTTP_TOTAL_TIMEOUT", 120.0),
            max_attempts=_env_int("BKG_HTTP_MAX_ATTEMPTS", 5),
            initial_backoff=_env_float("BKG_HTTP_INITIAL_BACKOFF", 1.0),
            max_backoff=_env_float("BKG_HTTP_MAX_BACKOFF", 30.0),
            user_agent=os.environ.get("BKG_HTTP_USER_AGENT", "backage"),
        )


@dataclass(frozen=True)
class GitHubTextRequestPolicy:
    """Per-operation retry and deadline limits for one text resource."""

    total_timeout: float
    max_attempts: int

    def __post_init__(self) -> None:
        if self.total_timeout <= 0:
            raise ValueError("text request total timeout must be positive")
        if self.max_attempts < 1:
            raise ValueError("text request attempts must be positive")


@dataclass(frozen=True)
class GitHubRuntime:
    """Injectable runtime hooks for graceful stopping and deterministic tests."""

    check_stop: Callable[[], None] = lambda: None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic


@dataclass(frozen=True)
class GitHubJsonResponse:
    """A decoded GitHub response with pagination metadata."""

    value: Any
    headers: httpx.Headers
    next_url: str | None = None


@dataclass(frozen=True)
class _JsonRequest:
    method: str
    url: str
    json_body: Mapping[str, object] | None = None
    authenticated: bool = True
    graphql: bool = False


class GitHubRateAccounting:
    """Persist REST and GraphQL usage in the existing bkg state file."""

    def __init__(self, state: StateStore) -> None:
        self.state = state

    def record_rest(self, headers: Mapping[str, str]) -> None:
        """Count one REST response and retain its latest rate-limit headers."""

        self._increment(1)
        self._record_rest_headers(headers)

    def record_graphql(
        self,
        value: object,
        headers: Mapping[str, str],
    ) -> None:
        """Count one GraphQL response using GitHub's reported query cost."""

        rate_limit = _graphql_rate_limit(value)
        cost = _positive_int(rate_limit.get("cost"), default=1)
        self._increment(cost)

        values: dict[str, str | int] = {"BKG_GRAPHQL_LAST_COST": cost}
        remaining = _nonnegative_int(rate_limit.get("remaining"))
        if remaining is not None:
            values["BKG_GRAPHQL_REMAINING"] = remaining
        reset_at = rate_limit.get("resetAt")
        if isinstance(reset_at, str) and reset_at:
            values["BKG_GRAPHQL_RESET_AT"] = reset_at
        self.state.set_many(values)
        self._record_rest_headers(headers)

    def _increment(self, amount: int) -> None:
        self.state.increment("BKG_CALLS_TO_API", amount)
        self.state.increment("BKG_MIN_CALLS_TO_API", amount)

    def _record_rest_headers(self, headers: Mapping[str, str]) -> None:
        values: dict[str, str | int] = {}
        remaining = _nonnegative_int(headers.get("x-ratelimit-remaining"))
        if remaining is not None:
            values["BKG_REST_REMAINING"] = remaining
        reset_at = _nonnegative_int(headers.get("x-ratelimit-reset"))
        if reset_at is not None:
            values["BKG_REST_RESET_AT"] = reset_at
        self.state.set_many(values)


class GitHubClient:
    """A reusable synchronous GitHub client with bounded retries."""

    def __init__(
        self,
        settings: GitHubSettings,
        *,
        accounting: GitHubRateAccounting | None = None,
        runtime: GitHubRuntime | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings
        self.accounting = accounting
        self.runtime = runtime or GitHubRuntime()
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=self._timeout(settings.total_timeout),
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
            ),
            follow_redirects=True,
        )

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the internally owned connection pool."""

        if self._owns_client:
            self._client.close()

    def rest_json(self, path: str) -> GitHubJsonResponse:
        """Request and decode one REST API path."""

        return self._request_json(_JsonRequest("GET", self._api_url(path)))

    def rest_json_optional(self, path: str) -> GitHubJsonResponse | None:
        """Request one REST path, returning None only for HTTP 404."""

        try:
            return self.rest_json(path)
        except GitHubNotFoundError:
            return None

    def get_text(  # pylint: disable=too-many-arguments
        self,
        url: str,
        *,
        authenticated: bool = False,
        accept: str = "text/html",
        bearer_token: str | None = None,
        policy: GitHubTextRequestPolicy | None = None,
    ) -> str:
        """Request one text resource through the shared retry policy."""

        total_timeout = (
            self.settings.total_timeout if policy is None else policy.total_timeout
        )
        max_attempts = (
            self.settings.max_attempts if policy is None else policy.max_attempts
        )
        deadline = self.runtime.clock() + total_timeout
        for attempt in range(1, max_attempts + 1):
            self.runtime.check_stop()
            try:
                response = self._client.get(
                    url,
                    headers=self._headers(
                        authenticated=authenticated,
                        accept=accept,
                        api_version=False,
                        bearer_token=bearer_token,
                    ),
                    timeout=self._timeout(self._remaining(deadline)),
                )
            except httpx.TransportError as error:
                if attempt >= max_attempts:
                    raise self._transport_error(url, error) from error
                self._sleep_before_retry(None, attempt, deadline)
                continue

            if self._should_retry(response) and attempt < max_attempts:
                self._sleep_before_retry(response, attempt, deadline)
                continue
            self._raise_for_status(response)
            return response.text

        raise GitHubTransportError("GitHub text request exhausted its retry budget")

    def rest_pages(self, path: str) -> Iterator[GitHubJsonResponse]:
        """Yield REST pages until GitHub no longer provides a next link."""

        next_url: str | None = self._api_url(path)
        while next_url is not None:
            response = self._request_json(_JsonRequest("GET", next_url))
            yield response
            next_url = response.next_url

    def graphql(self, query: str) -> GitHubJsonResponse:
        """Execute a GraphQL query and retain its reported rate cost."""

        query = _with_rate_limit(query)
        response = self._request_json(
            _JsonRequest(
                "POST",
                self._api_url("graphql"),
                json_body={"query": query},
                graphql=True,
            )
        )
        self._raise_for_graphql_errors(response.value)
        return response

    def download(
        self,
        url: str,
        destination: Path,
        *,
        authenticated: bool = False,
        default_mode: int = 0o644,
    ) -> None:
        """Stream a URL into an atomic destination."""

        deadline = self.runtime.clock() + self.settings.total_timeout
        for attempt in range(1, self.settings.max_attempts + 1):
            self.runtime.check_stop()
            try:
                with self._client.stream(
                    "GET",
                    url,
                    headers=self._headers(
                        authenticated=authenticated,
                        accept="application/octet-stream",
                    ),
                    timeout=self._timeout(self._remaining(deadline)),
                ) as response:
                    if self._should_retry(response) and (
                        attempt < self.settings.max_attempts
                    ):
                        self._sleep_before_retry(response, attempt, deadline)
                        continue
                    self._raise_for_status(response)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with atomic_binary_output(
                        destination,
                        default_mode=default_mode,
                    ) as file:
                        for chunk in response.iter_bytes():
                            self.runtime.check_stop()
                            file.write(chunk)
                    return
            except httpx.TransportError as error:
                if attempt >= self.settings.max_attempts:
                    raise self._transport_error("download", error) from error
                self._sleep_before_retry(None, attempt, deadline)

        raise GitHubTransportError("GitHub download exhausted its retry budget")

    def _request_json(self, request: _JsonRequest) -> GitHubJsonResponse:
        deadline = self.runtime.clock() + self.settings.total_timeout
        for attempt in range(1, self.settings.max_attempts + 1):
            self.runtime.check_stop()
            try:
                response = self._client.request(
                    request.method,
                    request.url,
                    headers=self._headers(authenticated=request.authenticated),
                    json=request.json_body,
                    timeout=self._timeout(self._remaining(deadline)),
                )
            except httpx.TransportError as error:
                if attempt >= self.settings.max_attempts:
                    raise self._transport_error(request.url, error) from error
                self._sleep_before_retry(None, attempt, deadline)
                continue

            if self._should_retry(response) and attempt < self.settings.max_attempts:
                self._record_json_response(request, response, None)
                self._sleep_before_retry(response, attempt, deadline)
                continue

            if not response.is_success:
                self._record_json_response(request, response, None)
                self._raise_for_status(response)
            try:
                value = response.json()
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
                self._record_json_response(request, response, None)
                raise GitHubDecodeError(
                    self._redact(
                        f"GitHub returned invalid JSON for {request.method} "
                        f"{request.url}"
                    )
                ) from error
            self._record_json_response(request, response, value)
            return GitHubJsonResponse(
                value=value,
                headers=response.headers,
                next_url=response.links.get("next", {}).get("url"),
            )

        raise GitHubTransportError("GitHub request exhausted its retry budget")

    def _api_url(self, path: str) -> str:
        if path.startswith(("https://", "http://")):
            return path
        return f"{self.settings.api_url.rstrip('/')}/{path.lstrip('/')}"

    def _record_json_response(
        self,
        request: _JsonRequest,
        response: httpx.Response,
        value: object | None,
    ) -> None:
        if self.accounting is None:
            return
        if request.graphql:
            self.accounting.record_graphql(value, response.headers)
        else:
            self.accounting.record_rest(response.headers)

    def _headers(
        self,
        *,
        authenticated: bool,
        accept: str = "application/vnd.github+json",
        api_version: bool = True,
        bearer_token: str | None = None,
    ) -> dict[str, str]:
        headers = {
            "Accept": accept,
            "User-Agent": self.settings.user_agent,
        }
        if api_version:
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        if bearer_token is not None:
            headers["Authorization"] = f"Bearer {bearer_token}"
        elif authenticated and self.settings.token:
            headers["Authorization"] = f"Bearer {self.settings.token}"
        return headers

    def _timeout(self, remaining: float) -> httpx.Timeout:
        return httpx.Timeout(
            connect=min(self.settings.connect_timeout, remaining),
            read=min(self.settings.read_timeout, remaining),
            write=min(self.settings.write_timeout, remaining),
            pool=min(self.settings.pool_timeout, remaining),
        )

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self.runtime.clock()
        if remaining <= 0:
            raise GitHubTransportError("GitHub operation exceeded its total timeout")
        return remaining

    def _should_retry(self, response: httpx.Response) -> bool:
        if response.status_code in _RETRYABLE_STATUS_CODES:
            return True
        if response.status_code != _FORBIDDEN_STATUS:
            return False
        if response.headers.get("x-ratelimit-remaining") == "0":
            return True
        body = response.text.lower()
        return any(marker in body for marker in _SECONDARY_LIMIT_MARKERS)

    def _sleep_before_retry(
        self,
        response: httpx.Response | None,
        attempt: int,
        deadline: float,
    ) -> None:
        delay = _response_retry_delay(response) if response is not None else None
        if delay is None:
            delay = min(
                self.settings.initial_backoff * (2 ** (attempt - 1)),
                self.settings.max_backoff,
            )
        delay = min(delay, self._remaining(deadline))
        self.runtime.sleep(delay)

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        body = _response_error_body(response)
        detail = f": {body}" if body else ""
        error_type = (
            GitHubNotFoundError
            if response.status_code == _NOT_FOUND_STATUS
            else GitHubResponseError
        )
        raise error_type(
            self._redact(
                f"GitHub returned HTTP {response.status_code} for "
                f"{response.request.method} {response.request.url}{detail}"
            ),
            status_code=response.status_code,
        )

    def _raise_for_graphql_errors(self, value: object) -> None:
        if not isinstance(value, dict):
            return
        errors = cast(dict[str, object], value).get("errors")
        if not isinstance(errors, list) or not errors:
            return

        messages: list[str] = []
        for error in cast(list[object], errors):
            if not isinstance(error, dict):
                continue
            message = cast(dict[str, object], error).get("message")
            if isinstance(message, str) and message:
                messages.append(message)
        detail = "; ".join(messages) or "unknown GraphQL error"
        if len(detail) > _ERROR_BODY_LIMIT:
            detail = f"{detail[:_ERROR_BODY_PREFIX]}..."
        raise GitHubGraphQLError(
            self._redact(f"GitHub GraphQL returned errors: {detail}")
        )

    def _transport_error(
        self,
        operation: str,
        error: httpx.TransportError,
    ) -> GitHubTransportError:
        return GitHubTransportError(
            self._redact(f"GitHub transport failed for {operation}: {error}")
        )

    def _redact(self, value: str) -> str:
        if not self.settings.token:
            return value
        return value.replace(self.settings.token, "[REDACTED]")


def _response_error_body(response: httpx.Response) -> str:
    body = response.text.strip()
    content_type = response.headers.get("content-type", "").casefold()
    if "text/html" in content_type or re.match(
        r"(?:<!doctype\s+html|<html)(?:\s|>)",
        body,
        flags=re.IGNORECASE,
    ):
        parser = _HtmlTitleParser()
        parser.feed(body)
        title = " ".join(" ".join(parser.parts).split())
        body = f"HTML response ({title})" if title else "HTML response"
    else:
        body = " ".join(body.split())
    if len(body) > _ERROR_BODY_LIMIT:
        body = f"{body[:_ERROR_BODY_PREFIX]}..."
    return body


def _with_rate_limit(query: str) -> str:
    query = query.strip()
    if not query:
        raise GitHubError("GraphQL query cannot be empty")
    if re.search(r"\brateLimit\b", query):
        return query
    closing_brace = query.rfind("}")
    if closing_brace < 0:
        raise GitHubError("GraphQL query must contain a selection set")
    return f"{query[:closing_brace]} {_RATE_LIMIT_SELECTION} {query[closing_brace:]}"


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
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _response_retry_delay(response: httpx.Response) -> float | None:
    retry_after = response.headers.get("retry-after")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass

    reset_at = response.headers.get("x-ratelimit-reset")
    if reset_at:
        try:
            return max(0.0, float(reset_at) - time.time())
        except ValueError:
            pass
    return None


def dump_json(value: object) -> str:
    """Serialize API output compactly without altering Unicode."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
