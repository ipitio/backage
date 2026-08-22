"""GitHub HTTP client, settings, errors, and rate accounting."""

from .client import (
    GitHubClient,
    GitHubJsonResponse,
    GitHubRuntime,
    GitHubTextRequestPolicy,
)
from .errors import (
    GitHubDecodeError,
    GitHubError,
    GitHubGraphQLError,
    GitHubNotFoundError,
    GitHubResponseError,
    GitHubTransportError,
)
from .rate import GitHubRateAccounting
from .settings import GitHubSettings

__all__ = [
    "GitHubClient",
    "GitHubDecodeError",
    "GitHubError",
    "GitHubGraphQLError",
    "GitHubJsonResponse",
    "GitHubNotFoundError",
    "GitHubRateAccounting",
    "GitHubResponseError",
    "GitHubRuntime",
    "GitHubSettings",
    "GitHubTextRequestPolicy",
    "GitHubTransportError",
]
