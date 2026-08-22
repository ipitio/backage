"""Validated settings for GitHub HTTP operations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from ..config import ConfigError, read_float, read_int, read_text
from ..runtime_names import EnvironmentVariable as Env

DEFAULT_REST_RESERVE = 50
AUTO_USER_AGENT = "auto"


def _positive_setting(
    values: Mapping[str, str],
    name: str,
    default: float,
) -> float:
    return read_float(
        values,
        name,
        default,
        minimum=0,
        minimum_exclusive=True,
    )


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
    rest_reserve: int = DEFAULT_REST_RESERVE
    user_agent: str = AUTO_USER_AGENT

    @property
    def user_agent_override(self) -> str | None:
        """Return an explicit all-request UA, or None for runtime resolution."""

        if self.user_agent == AUTO_USER_AGENT:
            return None
        return self.user_agent

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> GitHubSettings:
        """Load settings from one captured configuration mapping."""

        settings = cls(
            token=read_text(values, Env.GITHUB_TOKEN, "", allow_empty=True),
            api_url=read_text(
                values,
                Env.BKG_GITHUB_API_URL,
                "https://api.github.com",
            ),
            connect_timeout=_positive_setting(
                values, Env.BKG_HTTP_CONNECT_TIMEOUT, 10.0
            ),
            read_timeout=_positive_setting(values, Env.BKG_HTTP_READ_TIMEOUT, 60.0),
            write_timeout=_positive_setting(values, Env.BKG_HTTP_WRITE_TIMEOUT, 60.0),
            pool_timeout=_positive_setting(values, Env.BKG_HTTP_POOL_TIMEOUT, 10.0),
            total_timeout=_positive_setting(values, Env.BKG_HTTP_TOTAL_TIMEOUT, 120.0),
            max_attempts=read_int(
                values,
                Env.BKG_HTTP_MAX_ATTEMPTS,
                5,
                minimum=1,
            ),
            initial_backoff=_positive_setting(
                values, Env.BKG_HTTP_INITIAL_BACKOFF, 1.0
            ),
            max_backoff=_positive_setting(values, Env.BKG_HTTP_MAX_BACKOFF, 30.0),
            rest_reserve=read_int(
                values,
                Env.BKG_GITHUB_REST_RESERVE,
                DEFAULT_REST_RESERVE,
                minimum=0,
            ),
            user_agent=read_text(
                values,
                Env.BKG_HTTP_USER_AGENT,
                AUTO_USER_AGENT,
            ),
        )
        if settings.max_backoff < settings.initial_backoff:
            raise ConfigError(
                f"{Env.BKG_HTTP_MAX_BACKOFF} must be at least "
                f"{Env.BKG_HTTP_INITIAL_BACKOFF}"
            )
        return settings
