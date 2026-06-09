"""Read bkg's existing environment variables into a typed runtime config."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class RuntimeConfig:  # pylint: disable=too-many-instance-attributes
    """Runtime settings read from the environment used by the Bash entrypoints."""

    github_owner: str
    github_repo: str
    github_branch: str | None
    root: str
    env_file: str
    owners_file: str
    optout_file: str
    owners_table: str
    packages_table: str
    versions_table: str
    mode: int
    max_len: int
    is_first: str
    page_all: int
    index_name: str | None
    index_db: str | None
    index_sql: str | None
    index_dir: str | None

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        """Build a runtime configuration from the current process environment."""

        root = Path(os.environ.get("BKG_ROOT", _repo_root())).resolve()
        branch = os.environ.get("GITHUB_BRANCH")
        index_name = os.environ.get("BKG_INDEX")
        if index_name is None and branch:
            index_name = "index" if branch == "master" else f"index-{branch}"

        index_db = os.environ.get("BKG_INDEX_DB")
        index_sql = os.environ.get("BKG_INDEX_SQL")
        index_dir = os.environ.get("BKG_INDEX_DIR")
        if index_name:
            index_db = index_db or str(root / f"{index_name}.db")
            index_sql = index_sql or str(root / f"{index_name}.sql")
            index_dir = index_dir or str(root / index_name)

        return cls(
            github_owner=os.environ.get("GITHUB_OWNER", "ipitio"),
            github_repo=os.environ.get("GITHUB_REPO", "backage"),
            github_branch=branch,
            root=str(root),
            env_file=os.environ.get("BKG_ENV", str(root / "src" / "env.env")),
            owners_file=os.environ.get("BKG_OWNERS", str(root / "owners.txt")),
            optout_file=os.environ.get("BKG_OPTOUT", str(root / "optout.txt")),
            owners_table=os.environ.get("BKG_INDEX_TBL_OWN", "owners"),
            packages_table=os.environ.get("BKG_INDEX_TBL_PKG", "packages"),
            versions_table=os.environ.get("BKG_INDEX_TBL_VER", "versions"),
            mode=_env_int("BKG_MODE", 0),
            max_len=_env_int("BKG_MAX_LEN", 14400),
            is_first=os.environ.get("BKG_IS_FIRST", "false"),
            page_all=_env_int("BKG_PAGE_ALL", 1),
            index_name=index_name,
            index_db=index_db,
            index_sql=index_sql,
            index_dir=index_dir,
        )

    def as_dict(self) -> dict[str, int | str | None]:
        """Return a JSON-serializable representation of this configuration."""

        return asdict(self)
