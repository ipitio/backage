"""Bounded package API emptiness checks with run-scoped capability memory."""

from collections.abc import Callable, Generator
from contextlib import contextmanager
from threading import Semaphore
from typing import Protocol, cast
from urllib.parse import quote, urlencode

from ..github import GitHubError, GitHubJsonResponse, GitHubResponseError

_API_PACKAGE_TYPES = ("container", "npm", "maven", "rubygems", "nuget", "docker")
_GATE_POLL_SECONDS = 0.1
_UNSUPPORTED_STATUS = 400


class PackageInventoryClient(Protocol):  # pylint: disable=too-few-public-methods
    """GitHub operation needed to verify readable package inventory."""

    def rest_json_optional(self, path: str) -> GitHubJsonResponse | None:
        """Return one JSON response or an absent-resource marker."""

        raise NotImplementedError


class PackageInventoryProbe:  # pylint: disable=too-few-public-methods
    """Check every ecosystem without repeating known installation-token failures."""

    def __init__(
        self,
        *,
        installation_token: bool = False,
        check_stop: Callable[[], None] = lambda: None,
    ) -> None:
        self._installation_token = installation_token
        self._check_stop = check_stop
        self._gate = Semaphore(1)
        self._unavailable_scopes: set[tuple[str, str | None]] = set()

    def verify_empty(
        self,
        client: PackageInventoryClient,
        owner_type: str,
        owner: str,
        *,
        visibility: str | None,
    ) -> tuple[bool, str]:
        """Confirm all empty first pages, or explain why inventory remains unknown."""

        with self._verification_slot():
            self._check_stop()
            scope = (owner_type, visibility)
            if scope in self._unavailable_scopes:
                return False, self._unavailable_diagnostic(scope)
            try:
                return self._verify(client, owner_type, owner, visibility)
            except GitHubResponseError as error:
                if (
                    self._installation_token
                    and error.status_code == _UNSUPPORTED_STATUS
                    and "invalid argument" in str(error).casefold()
                ):
                    self._unavailable_scopes.add(scope)
                    return False, self._unavailable_diagnostic(scope)
                return False, f"Package API inventory verification failed: {error}"
            except GitHubError as error:
                return False, f"Package API inventory verification failed: {error}"

    @contextmanager
    def _verification_slot(self) -> Generator[None]:
        while not self._gate.acquire(timeout=_GATE_POLL_SECONDS):
            self._check_stop()
        try:
            yield
        finally:
            self._gate.release()

    def _verify(
        self,
        client: PackageInventoryClient,
        owner_type: str,
        owner: str,
        visibility: str | None,
    ) -> tuple[bool, str]:
        owner_path = f"{owner_type}/{quote(owner, safe='')}/packages"
        for package_type in _API_PACKAGE_TYPES:
            self._check_stop()
            query: list[tuple[str, str | int]] = [
                ("package_type", package_type),
                ("per_page", 1),
                ("page", 1),
            ]
            if visibility is not None:
                query.append(("visibility", visibility))
            response = client.rest_json_optional(f"{owner_path}?{urlencode(query)}")
            if response is None:
                return False, f"Package API unavailable for {package_type}"
            value: object = response.value
            if not isinstance(value, list) or response.next_url is not None:
                return (
                    False,
                    f"Package API did not establish empty inventory for {package_type}",
                )
            if cast(list[object], value):
                return (
                    False,
                    f"Package API returned nonempty inventory for {package_type}",
                )
        return True, f"Verified empty package listing for {owner} via package API"

    @staticmethod
    def _unavailable_diagnostic(scope: tuple[str, str | None]) -> str:
        owner_type, visibility = scope
        return (
            f"Package API inventory verification unavailable for {owner_type} "
            f"visibility={visibility or 'all'} this run: GitHub rejected this "
            "installation token with HTTP 400 Invalid argument; "
            "retaining unverified inventory and using HTML discovery"
        )
