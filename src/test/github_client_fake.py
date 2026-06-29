"""Shared in-memory GitHub client for package and version tests."""

from __future__ import annotations

from collections.abc import Callable, Mapping

import httpx

from bkg_py.github import GitHubJsonResponse, GitHubTextRequestPolicy


class FakeGitHubClient:
    """Return configured REST and public HTML responses while recording requests."""

    def __init__(
        self,
        *,
        rest_values: Mapping[str, object] | None = None,
        text_values: Mapping[str, str | Exception | Callable[[], str]] | None = None,
    ) -> None:
        self.rest_values = dict(rest_values or {})
        self.text_values = dict(text_values or {})
        self.rest_requests: list[str] = []
        self.text_requests: list[str] = []
        self.text_authentication: list[bool] = []
        self.text_policies: list[GitHubTextRequestPolicy | None] = []

    def rest_json(self, path: str) -> GitHubJsonResponse:
        """Return one configured REST response."""

        self.rest_requests.append(path)
        return GitHubJsonResponse(self.rest_values[path], httpx.Headers())

    def rest_json_optional(self, path: str) -> GitHubJsonResponse | None:
        """Return a configured REST response or an absent-resource marker."""

        self.rest_requests.append(path)
        value = self.rest_values[path]
        if value is None:
            return None
        return GitHubJsonResponse(value, httpx.Headers())

    def get_text(
        self,
        url: str,
        *,
        authenticated: bool = False,
        accept: str = "text/html",
        policy: GitHubTextRequestPolicy | None = None,
    ) -> str:
        """Return one configured public text response."""

        if accept != "text/html":
            raise ValueError(f"unsupported fake response type: {accept}")
        self.text_requests.append(url)
        self.text_authentication.append(authenticated)
        self.text_policies.append(policy)
        value = self.text_values[url]
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value()
        return value
