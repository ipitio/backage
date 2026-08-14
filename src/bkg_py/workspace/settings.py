"""Immutable settings for repository workspace operations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace

from ..config import (
    RepositoryIdentity,
    SettingsSnapshot,
    read_float,
    read_optional_text,
    read_text,
)
from ..runtime_names import EnvironmentVariable as Env

_DEFAULT_HANDOFF_ACTOR = "github-actions[bot]"
_DEFAULT_HANDOFF_EMAIL_ACTOR = "41898282+github-actions[bot]"


@dataclass(frozen=True)
class GitIdentity:
    """Commit author and committer identity for one Git operation."""

    name: str
    email: str

    @classmethod
    def for_actor(cls, actor: str) -> GitIdentity:
        """Build the workflow identity for one nonempty GitHub actor."""

        if not actor:
            raise ValueError("Git actor must not be empty")
        return cls(actor, f"{actor}@users.noreply.github.com")

    @classmethod
    def github_actions(cls) -> GitIdentity:
        """Return GitHub's standard Actions bot identity."""

        return cls(
            _DEFAULT_HANDOFF_ACTOR,
            f"{_DEFAULT_HANDOFF_EMAIL_ACTOR}@users.noreply.github.com",
        )

    def environment(self) -> dict[str, str]:
        """Return the Git variables used by plumbing commands."""

        return {
            "GIT_AUTHOR_NAME": self.name,
            "GIT_AUTHOR_EMAIL": self.email,
            "GIT_COMMITTER_NAME": self.name,
            "GIT_COMMITTER_EMAIL": self.email,
        }


@dataclass(frozen=True)
class HandoffSettings:
    """Control-ref settings shared by requesters and active-run monitors."""

    control_ref: str
    poll_seconds: float = 60
    git_timeout_seconds: float = 20
    identity: GitIdentity = field(default_factory=GitIdentity.github_actions)
    run_id: str = "manual"

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, str],
        *,
        identity: GitIdentity | None = None,
    ) -> HandoffSettings:
        """Read handoff controls from one captured mapping."""

        actor = read_optional_text(values, Env.GITHUB_ACTOR)
        return cls(
            control_ref=read_text(
                values,
                Env.BKG_HANDOFF_CONTROL_REF,
                "",
                allow_empty=True,
            ),
            poll_seconds=read_float(
                values,
                Env.BKG_HANDOFF_POLL_SECONDS,
                60,
                minimum=0,
                minimum_exclusive=True,
            ),
            git_timeout_seconds=read_float(
                values,
                Env.BKG_HANDOFF_GIT_TIMEOUT_SECONDS,
                20,
                minimum=0,
                minimum_exclusive=True,
            ),
            identity=identity
            or (
                GitIdentity.for_actor(actor) if actor else GitIdentity.github_actions()
            ),
            run_id=read_optional_text(values, Env.GITHUB_RUN_ID) or "manual",
        )

    def as_dict(self) -> dict[str, object]:
        """Return non-secret handoff settings for diagnostics."""

        return asdict(self)


@dataclass(frozen=True)
class WorkspaceSettings:
    """Repository and Git settings resolved for one workspace operation."""

    source: SettingsSnapshot = field(repr=False)
    repository: RepositoryIdentity
    identity: GitIdentity
    handoff: HandoffSettings
    token: str = field(repr=False)
    token_source: str

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> WorkspaceSettings:
        """Compose workspace settings from one supplied mapping."""

        source = (
            values if isinstance(values, SettingsSnapshot) else SettingsSnapshot(values)
        )
        repository = RepositoryIdentity.from_mapping(source)
        actor = read_optional_text(source, Env.GITHUB_ACTOR) or repository.owner
        identity = GitIdentity.for_actor(actor)
        token = read_text(source, Env.GITHUB_TOKEN, "", allow_empty=True)
        return cls(
            source=source,
            repository=repository,
            identity=identity,
            handoff=HandoffSettings.from_mapping(source, identity=identity),
            token=token,
            token_source="environment" if token else "none",
        )

    def with_token(self, token: str, source: str) -> WorkspaceSettings:
        """Return settings with one resolved credential and its source."""

        return replace(
            self,
            token=token,
            token_source=source if token else "none",
        )

    def resolved_mapping(
        self,
        overrides: Mapping[str, str] | None = None,
    ) -> SettingsSnapshot:
        """Return the captured environment plus resolved workflow values."""

        values = dict(self.source)
        values.update(
            {
                Env.GITHUB_TOKEN: self.token,
                Env.GITHUB_ACTOR: self.identity.name,
                Env.GITHUB_OWNER: self.repository.owner,
                Env.GITHUB_REPO: self.repository.name,
            }
        )
        if self.repository.branch is None:
            values.pop(Env.GITHUB_BRANCH, None)
        else:
            values[Env.GITHUB_BRANCH] = self.repository.branch
        if overrides is not None:
            values.update(overrides)
        return SettingsSnapshot(values)

    def redacted_values(self) -> tuple[str, ...]:
        """Return captured credentials that must not appear in Git diagnostics."""

        candidates = (
            self.token,
            self.source.get(Env.GITHUB_TOKEN, ""),
            self.source.get(Env.GH_TOKEN, ""),
        )
        return tuple(dict.fromkeys(value for value in candidates if value))

    def as_dict(self) -> dict[str, object]:
        """Return effective non-secret workspace settings for diagnostics."""

        return {
            "repository": asdict(self.repository),
            "git_identity": asdict(self.identity),
            "handoff": self.handoff.as_dict(),
            "token_configured": bool(self.token),
            "token_source": self.token_source,
        }
