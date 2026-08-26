"""Errors raised by GitHub API and public-resource operations."""


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
