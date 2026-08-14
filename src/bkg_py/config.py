"""Capture and parse bkg's process configuration."""

from __future__ import annotations

import math
import os
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import MappingProxyType

from .runtime_names import EnvironmentVariable as Env

DEFAULT_GITHUB_OWNER = "ipitio"
DEFAULT_GITHUB_REPOSITORY = "backage"


class ConfigError(ValueError):
    """An explicitly supplied configuration value is invalid."""


@dataclass(frozen=True)
class SettingsSnapshot(Mapping[str, str]):
    """Immutable copy of one command's process configuration."""

    _values: Mapping[str, str] = field(repr=False)

    def __init__(self, values: Mapping[str, str]) -> None:
        object.__setattr__(self, "_values", MappingProxyType(dict(values)))

    @classmethod
    def from_env(cls) -> SettingsSnapshot:
        """Capture the current process environment once."""

        return cls(os.environ)

    def __getitem__(self, key: str) -> str:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


def read_text(
    values: Mapping[str, str],
    name: str,
    default: str,
    *,
    allow_empty: bool = False,
) -> str:
    """Read text, rejecting an explicitly empty value unless allowed."""

    value = values.get(name)
    if value is None:
        return default
    if not value and not allow_empty:
        raise ConfigError(f"{name} must not be empty")
    return value


def read_optional_text(values: Mapping[str, str], name: str) -> str | None:
    """Read optional text, treating an empty shell value as absent."""

    return values.get(name) or None


def read_int(
    values: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Read an integer within an optional inclusive range."""

    value = values.get(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise ConfigError(f"{name} must be an integer") from error
    if minimum is not None and parsed < minimum:
        raise ConfigError(_range_error(name, minimum, maximum))
    if maximum is not None and parsed > maximum:
        raise ConfigError(_range_error(name, minimum, maximum))
    return parsed


def read_optional_int(values: Mapping[str, str], name: str) -> int | None:
    """Read an optional signed integer."""

    if name not in values:
        return None
    return read_int(values, name, 0)


def read_float(
    values: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    minimum_exclusive: bool = False,
) -> float:
    """Read a finite float with an optional lower bound."""

    value = values.get(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError as error:
        raise ConfigError(f"{name} must be a number") from error
    if not math.isfinite(parsed):
        raise ConfigError(f"{name} must be a finite number")
    below_minimum = minimum is not None and parsed < minimum
    at_exclusive_minimum = (
        minimum is not None and minimum_exclusive and parsed == minimum
    )
    if below_minimum or at_exclusive_minimum:
        qualifier = "greater than" if minimum_exclusive else "at least"
        raise ConfigError(f"{name} must be {qualifier} {minimum:g}")
    return parsed


def read_bool(values: Mapping[str, str], name: str, default: bool) -> bool:
    """Read one explicit shell-style boolean."""

    value = values.get(name)
    if value is None:
        return default
    normalized = value.casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be one of: 1, 0, true, false, yes, no, on, off")


def _range_error(
    name: str,
    minimum: int | None,
    maximum: int | None,
) -> str:
    if minimum is not None and maximum is not None:
        return f"{name} must be between {minimum} and {maximum}"
    if minimum is not None:
        return f"{name} must be at least {minimum}"
    if maximum is not None:
        return f"{name} must be at most {maximum}"
    raise AssertionError("range error requires a bound")


def _repo_root() -> Path:
    working_directory = Path.cwd().resolve()
    for candidate in (working_directory, *working_directory.parents):
        if (candidate / "src" / "bkg_py").is_dir():
            return candidate
    return working_directory


def _default_parallel_jobs() -> int:
    return max(1, (os.process_cpu_count() or 1) * 2)


@dataclass(frozen=True)
class RepositoryIdentity:
    """GitHub repository coordinates captured for one operation."""

    owner: str
    name: str
    branch: str | None

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> RepositoryIdentity:
        """Read repository coordinates from one captured mapping."""

        return cls(
            owner=read_text(values, Env.GITHUB_OWNER, DEFAULT_GITHUB_OWNER),
            name=read_text(values, Env.GITHUB_REPO, DEFAULT_GITHUB_REPOSITORY),
            branch=read_optional_text(values, Env.GITHUB_BRANCH),
        )


@dataclass(frozen=True)
class RepositoryMaintenanceSettings:
    """Optional repository coordinates for an explicit maintenance command."""

    owner: str | None
    name: str | None

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, str],
    ) -> RepositoryMaintenanceSettings:
        """Read maintenance coordinates without selecting a default target."""

        return cls(
            owner=read_optional_text(values, Env.GITHUB_OWNER),
            name=read_optional_text(values, Env.GITHUB_REPO),
        )


@dataclass(frozen=True)
class RuntimeConfig:  # pylint: disable=too-many-instance-attributes
    """Runtime settings read from the process environment."""

    github_owner: str
    github_repo: str
    github_branch: str | None
    root: str
    env_file: str
    owners_file: str
    optout_file: str
    owner_id_cache_file: str
    owners_table: str
    packages_table: str
    versions_table: str
    mode: int
    max_len: int
    is_first: str
    index_name: str | None
    index_db: str | None
    index_sql: str | None
    index_dir: str | None
    max_version_pages: int = 3
    tag_cache_pages: int = 3
    append_tagged_versions_limit: int = 30
    owner_discovery_max_pages: int = 1
    snapshot_rotation_threshold_bytes: int = 2_000_000_000
    parallel_async_max_jobs: int = field(default_factory=_default_parallel_jobs)
    owner_update_stop_grace: float = 180.0
    docker_size_fallback: bool = False
    docker_platform: str = "linux/amd64"
    docker_pull_timeout: float = 300.0
    docker_command_timeout: float = 30.0

    @classmethod
    def from_env(cls) -> RuntimeConfig:
        """Build a runtime configuration from the current process environment."""

        return cls.from_mapping(SettingsSnapshot.from_env())

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> RuntimeConfig:
        """Build runtime configuration from one captured mapping."""

        root = Path(read_text(values, Env.BKG_ROOT, str(_repo_root()))).resolve()
        repository = RepositoryIdentity.from_mapping(values)
        branch = repository.branch
        index_name = read_optional_text(values, Env.BKG_INDEX)
        if index_name is None and branch:
            index_name = "index" if branch == "master" else f"index-{branch}"

        index_db = read_optional_text(values, Env.BKG_INDEX_DB)
        index_sql = read_optional_text(values, Env.BKG_INDEX_SQL)
        index_dir = read_optional_text(values, Env.BKG_INDEX_DIR)
        if index_name:
            index_db = index_db or str(root / f"{index_name}.db")
            index_sql = index_sql or str(root / f"{index_name}.sql")
            index_dir = index_dir or str(root / index_name)

        env_file = read_text(values, Env.BKG_ENV, str(root / "src" / "env.env"))

        return cls(
            github_owner=repository.owner,
            github_repo=repository.name,
            github_branch=branch,
            root=str(root),
            env_file=env_file,
            owners_file=read_text(values, Env.BKG_OWNERS, str(root / "owners.txt")),
            optout_file=read_text(values, Env.BKG_OPTOUT, str(root / "optout.txt")),
            owner_id_cache_file=read_text(
                values,
                Env.BKG_OWNER_ID_CACHE,
                str(Path(env_file).parent / "owner-id-cache.txt"),
            ),
            owners_table=read_text(values, Env.BKG_INDEX_TBL_OWN, "owners"),
            packages_table=read_text(values, Env.BKG_INDEX_TBL_PKG, "packages"),
            versions_table=read_text(values, Env.BKG_INDEX_TBL_VER, "versions"),
            mode=read_int(values, Env.BKG_MODE, 0, minimum=0, maximum=5),
            max_len=read_int(values, Env.BKG_MAX_LEN, 14400),
            is_first=str(read_bool(values, Env.BKG_IS_FIRST, False)).lower(),
            index_name=index_name,
            index_db=index_db,
            index_sql=index_sql,
            index_dir=index_dir,
            max_version_pages=read_int(
                values,
                Env.BKG_MAX_VERSION_PAGES,
                3,
                minimum=0,
            ),
            tag_cache_pages=read_int(
                values,
                Env.BKG_TAG_CACHE_PAGES,
                3,
                minimum=0,
            ),
            append_tagged_versions_limit=read_int(
                values,
                Env.BKG_APPEND_TAGGED_VERSIONS_LIMIT,
                30,
                minimum=0,
            ),
            owner_discovery_max_pages=read_int(
                values,
                Env.BKG_OWNER_DISCOVERY_MAX_PAGES,
                1,
                minimum=0,
            ),
            snapshot_rotation_threshold_bytes=read_int(
                values,
                Env.BKG_SNAPSHOT_ROTATION_THRESHOLD_BYTES,
                2_000_000_000,
                minimum=1,
            ),
            parallel_async_max_jobs=read_int(
                values,
                Env.BKG_PARALLEL_ASYNC_MAX_JOBS,
                _default_parallel_jobs(),
                minimum=1,
            ),
            owner_update_stop_grace=read_float(
                values,
                Env.BKG_OWNER_UPDATE_STOP_GRACE,
                180.0,
                minimum=0,
                minimum_exclusive=True,
            ),
            docker_size_fallback=read_bool(
                values,
                Env.BKG_DOCKER_SIZE_FALLBACK,
                False,
            ),
            docker_platform=read_text(
                values,
                Env.BKG_DOCKER_PLATFORM,
                "linux/amd64",
            ),
            docker_pull_timeout=read_float(
                values,
                Env.BKG_DOCKER_PULL_TIMEOUT,
                300.0,
                minimum=0,
                minimum_exclusive=True,
            ),
            docker_command_timeout=read_float(
                values,
                Env.BKG_DOCKER_COMMAND_TIMEOUT,
                30.0,
                minimum=0,
                minimum_exclusive=True,
            ),
        )

    def as_dict(self) -> dict[str, bool | float | int | str | None]:
        """Return a JSON-serializable representation of this configuration."""

        return asdict(self)
