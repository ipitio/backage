"""Owner identity helpers for GitHub discovery."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Generator, Iterable, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

from .config import RuntimeConfig
from .files import atomic_text_output
from .github import GitHubClient, GitHubError

_OWNER_REF_PATTERN = re.compile(r"^[1-9][0-9]*/.+$")
_OWNER_LOOKUP_BATCH_SIZE = 50
_REST_OWNER_LOOKUP_PREFIXES = ("users", "orgs")
_REPOSITORY_GRAPHQL_EDGES = frozenset({"stargazers", "watchers", "forks"})
_REPOSITORY_DISCOVERY_EDGES = ("stargazers", "watchers", "forks", "collaborators")
_OWNER_DISCOVERY_EDGES = ("followers", "following", "people")


class DiscoveryError(RuntimeError):
    """Discovery-owned identity resolution failed."""


@dataclass(frozen=True)
class OwnerIdentity:
    """A canonical GitHub owner reference."""

    owner_id: str
    login: str

    @property
    def ref(self) -> str:
        """Return the shell-compatible ID/login representation."""

        return f"{self.owner_id}/{self.login}"


@dataclass(frozen=True)
class OwnerLookupResult:
    """The result of looking up one owner name."""

    owner_ref: str | None
    missing: bool = False


@dataclass(frozen=True)
class DiscoveryPage:
    """One page of owner logins discovered through GitHub GraphQL."""

    nodes: tuple[str, ...]
    has_next_page: bool = False
    end_cursor: str = ""


@dataclass
class _CandidateResolutionState:
    resolved_by_owner: dict[str, str]
    missing_by_owner: set[str]
    unresolved: list[str]


def is_owner_ref(value: str) -> bool:
    """Return whether a value already has the ID/login owner shape."""

    return _OWNER_REF_PATTERN.fullmatch(value.strip()) is not None


def owner_ref_login(value: str) -> str:
    """Return the login portion of an owner name or ID/login reference."""

    candidate = value.strip()
    if is_owner_ref(candidate):
        return candidate.split("/", maxsplit=1)[1]
    return candidate.split("/", maxsplit=1)[-1]


def _owner_ref_key(value: str) -> str:
    return owner_ref_login(value).casefold()


@dataclass(frozen=True)
class OwnerIdentityCache:
    """Persist run-scoped owner ID lookups in the existing cache file."""

    path: Path
    lock_poll_interval: float = 0.05

    @classmethod
    def from_config(cls, config: RuntimeConfig) -> OwnerIdentityCache:
        """Build the cache path from shell-compatible runtime settings."""

        return cls(Path(config.owner_id_cache_file))

    @property
    def _lock_path(self) -> Path:
        return Path(f"{self.path}.lock")

    @contextmanager
    def _lock(self) -> Generator[None, None, None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        while True:
            try:
                os.link(self.path, self._lock_path)
                break
            except FileExistsError:
                time.sleep(self.lock_poll_interval)

        try:
            yield
        finally:
            with suppress(FileNotFoundError):
                self._lock_path.unlink()

    def _read_refs(self) -> list[str]:
        try:
            return self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []

    def lookup(self, value: str) -> str | None:
        """Return an unambiguous cached owner reference for a login."""

        login_key = _owner_ref_key(value)
        if not login_key:
            return None

        matched: str | None = None
        for line in self._read_refs():
            if not is_owner_ref(line) or _owner_ref_key(line) != login_key:
                continue
            if matched is not None and matched != line:
                return None
            matched = line
        return matched

    def cache(self, owner_ref: str) -> None:
        """Store a resolved owner reference, replacing stale refs for the login."""

        if not is_owner_ref(owner_ref):
            return
        login_key = _owner_ref_key(owner_ref)

        with self._lock():
            retained = [
                line
                for line in self._read_refs()
                if not is_owner_ref(line) or _owner_ref_key(line) != login_key
            ]
            retained.append(owner_ref.strip())
            seen: set[str] = set()
            unique: list[str] = []
            for line in retained:
                if line in seen:
                    continue
                seen.add(line)
                unique.append(line)
            with atomic_text_output(self.path) as file:
                if unique:
                    file.write("".join(f"{line}\n" for line in unique))


class OwnerIdentityResolver:
    """Resolve owner names through cache, GraphQL batches, and REST fallback."""

    def __init__(self, cache: OwnerIdentityCache, client: GitHubClient) -> None:
        self.cache = cache
        self.client = client

    def owner_type(self, value: str) -> str | None:
        """Return GitHub's owner typename for a login, when it exists."""

        login = owner_ref_login(value)
        if not login:
            return None
        response = self.client.graphql(_owner_type_query(login))
        owner = _data_value(response.value, "owner")
        if not isinstance(owner, dict):
            return None
        typename = cast(dict[str, object], owner).get("__typename")
        return typename if isinstance(typename, str) and typename else None

    def resolve_owner(self, value: str) -> OwnerLookupResult:
        """Resolve one owner to an ID/login reference."""

        candidate = value.strip()
        if not candidate:
            return OwnerLookupResult(None)
        if is_owner_ref(candidate):
            self.cache.cache(candidate)
            return OwnerLookupResult(candidate)

        cached = self.cache.lookup(candidate)
        if cached is not None:
            return OwnerLookupResult(cached)

        return self._rest_owner_lookup(owner_ref_login(candidate))

    def resolve_candidate_file(
        self,
        candidates_path: Path,
        *,
        missing_path: Path | None = None,
    ) -> list[str]:
        """Resolve owner candidates from a file and optionally write missing names."""

        candidates = [
            line
            for line in candidates_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        if not candidates:
            return []
        if missing_path is not None:
            missing_path.parent.mkdir(parents=True, exist_ok=True)
            missing_path.write_text("", encoding="utf-8")

        state = self._collect_candidate_state(candidates)
        if state.unresolved and self.client.settings.token:
            self._resolve_graphql_batches(
                state.unresolved,
                state.resolved_by_owner,
                state.missing_by_owner,
            )

        output, missing_output = self._resolved_candidate_output(candidates, state)
        if missing_path is not None:
            missing_path.write_text(
                "".join(f"{owner}\n" for owner in missing_output),
                encoding="utf-8",
            )
        return output

    def _collect_candidate_state(
        self,
        candidates: Iterable[str],
    ) -> _CandidateResolutionState:
        state = _CandidateResolutionState({}, set(), [])
        for candidate in candidates:
            login = owner_ref_login(candidate)
            if not login:
                continue
            if is_owner_ref(candidate):
                state.resolved_by_owner[login] = candidate
                self.cache.cache(candidate)
                continue

            cached = self.cache.lookup(login)
            if cached is not None:
                state.resolved_by_owner[login] = cached
            elif login not in state.resolved_by_owner:
                state.unresolved.append(login)
        return state

    def _resolved_candidate_output(
        self,
        candidates: Iterable[str],
        state: _CandidateResolutionState,
    ) -> tuple[list[str], list[str]]:
        output: list[str] = []
        emitted_refs: set[str] = set()
        missing_output: list[str] = []
        for candidate in candidates:
            if is_owner_ref(candidate):
                output.append(candidate)
                continue

            login = owner_ref_login(candidate)
            lookup = self._candidate_lookup_result(login, state)
            if lookup.missing:
                if login:
                    missing_output.append(login)
                continue

            resolved = lookup.owner_ref
            if (
                resolved is None
                or not is_owner_ref(resolved)
                or resolved in emitted_refs
            ):
                continue
            emitted_refs.add(resolved)
            output.append(resolved)
        return output, missing_output

    def _candidate_lookup_result(
        self,
        login: str,
        state: _CandidateResolutionState,
    ) -> OwnerLookupResult:
        if not login:
            return OwnerLookupResult(None)
        if login in state.missing_by_owner:
            return OwnerLookupResult(None, missing=True)
        resolved = state.resolved_by_owner.get(login)
        if resolved is not None:
            return OwnerLookupResult(resolved)
        try:
            return self.resolve_owner(login)
        except GitHubError:
            return OwnerLookupResult(None)

    def repository_nodes(
        self,
        owner: str,
        repo: str,
        edge: str,
        cursor: str = "",
    ) -> DiscoveryPage:
        """Return one GraphQL discovery page for a repository edge."""

        if edge not in {"stargazers", "watchers", "forks"}:
            raise DiscoveryError(f"unsupported repository discovery edge: {edge}")
        response = self.client.graphql(
            _repository_discovery_query(owner, repo, edge, cursor)
        )
        repository = _data_value(response.value, "repository")
        if not isinstance(repository, dict):
            raise DiscoveryError(f"repository not found for {owner}/{repo}")
        connection = cast(dict[str, object], repository).get(edge)
        if not isinstance(connection, dict):
            raise DiscoveryError(f"missing repository discovery edge: {edge}")
        return self._connection_page(cast(dict[str, object], connection), edge=edge)

    def owner_nodes(
        self,
        owner_ref: str,
        edge: str,
        cursor: str = "",
        owner_type: str = "",
    ) -> DiscoveryPage:
        """Return one GraphQL discovery page for an owner edge."""

        login = owner_ref_login(owner_ref)
        resolved_type = owner_type or self.owner_type(login)
        if not resolved_type:
            raise DiscoveryError(f"owner type not found for {login}")

        if edge in {"followers", "following", "organizations"}:
            if resolved_type != "User":
                return DiscoveryPage(())
            connection_name = edge
        elif edge == "people":
            if resolved_type != "Organization":
                return DiscoveryPage(())
            connection_name = "membersWithRole"
        else:
            raise DiscoveryError(f"unsupported owner discovery edge: {edge}")

        response = self.client.graphql(
            _owner_discovery_query(login, resolved_type, edge, cursor)
        )
        owner = _data_value(response.value, "owner")
        if not isinstance(owner, dict):
            raise DiscoveryError(f"owner not found for {login}")
        connection = cast(dict[str, object], owner).get(connection_name)
        if not isinstance(connection, dict):
            raise DiscoveryError(f"missing owner discovery edge: {edge}")
        return self._connection_page(cast(dict[str, object], connection), edge=edge)

    def organization_logins(
        self, owner_ref: str, *, resolve: bool = False
    ) -> tuple[str, ...]:
        """Return organizations associated with a user login."""

        login = owner_ref_login(owner_ref)
        owner_type = self.owner_type(login)
        if owner_type == "Organization":
            return ()
        if owner_type != "User":
            raise DiscoveryError(f"owner type not found for {login}")

        organizations = self._paged_owner_nodes(login, "organizations", owner_type)
        if not resolve:
            return organizations
        return self._resolve_logins(organizations)

    def explore(self, node: str, edge: str = "") -> tuple[str, ...]:
        """Traverse one authenticated discovery target."""

        if "/" in node:
            return self._explore_repository(node, edge)
        return self._explore_owner(node, edge)

    def membership(self, owner_ref: str) -> tuple[str, ...]:
        """Return members or organizations for the configured GitHub owner."""

        owner = owner_ref_login(owner_ref)
        owner_type = self.owner_type(owner)
        if owner_type == "Organization":
            return self._paged_owner_nodes(owner, "people", owner_type)
        if owner_type == "User":
            return self.organization_logins(owner)
        raise DiscoveryError(f"owner type not found for {owner}")

    def _resolve_graphql_batches(
        self,
        unresolved: Sequence[str],
        resolved_by_owner: dict[str, str],
        missing_by_owner: set[str],
    ) -> None:
        for batch in _chunks(_unique(unresolved), _OWNER_LOOKUP_BATCH_SIZE):
            try:
                response = self.client.graphql(_owner_lookup_query(batch))
            except GitHubError:
                continue
            identities = _owner_lookup_identities(response.value, batch)
            if identities is None:
                continue
            for login, identity in zip(batch, identities, strict=True):
                if identity is None:
                    missing_by_owner.add(login)
                else:
                    resolved_by_owner[login] = identity.ref
                    self.cache.cache(identity.ref)

    def _rest_owner_lookup(self, login: str) -> OwnerLookupResult:
        if not login:
            return OwnerLookupResult(None)

        missing_responses = 0
        for prefix in _REST_OWNER_LOOKUP_PREFIXES:
            path = f"{prefix}/{quote(login, safe='')}"
            response = self.client.rest_json_optional(path)
            if response is None:
                missing_responses += 1
                continue
            identity = _rest_owner_identity(response.value, fallback_login=login)
            if identity is not None:
                self.cache.cache(identity.ref)
                return OwnerLookupResult(identity.ref)

        return OwnerLookupResult(
            None,
            missing=missing_responses == len(_REST_OWNER_LOOKUP_PREFIXES),
        )

    def _explore_repository(self, node: str, edge: str) -> tuple[str, ...]:
        owner, repo = _repository_parts(node)
        if not owner or not repo:
            raise DiscoveryError(f"invalid repository discovery target: {node}")
        edges = (edge,) if edge else _REPOSITORY_DISCOVERY_EDGES

        output: list[str] = []
        for current_edge in edges:
            if current_edge in _REPOSITORY_GRAPHQL_EDGES:
                output.extend(
                    _without_self(
                        self._paged_repository_nodes(owner, repo, current_edge),
                        owner,
                    )
                )
            elif current_edge == "collaborators":
                output.extend(
                    _without_self(
                        self._repository_collaborators(owner, repo),
                        owner,
                    )
                )
            else:
                raise DiscoveryError(
                    f"unsupported repository discovery edge: {current_edge}"
                )
        return tuple(output)

    def _explore_owner(self, owner_ref: str, edge: str) -> tuple[str, ...]:
        login = owner_ref_login(owner_ref)
        owner_type = self.owner_type(login)
        if not owner_type:
            raise DiscoveryError(f"owner type not found for {login}")
        edges = (edge,) if edge else _OWNER_DISCOVERY_EDGES
        output: list[str] = []
        got_orgs = False

        for current_edge in edges:
            output.extend(
                _without_self(
                    self._paged_owner_nodes(login, current_edge, owner_type),
                    login,
                )
            )
            if owner_type == "User" and not got_orgs:
                output.extend(self.organization_logins(login))
                got_orgs = True
        return tuple(output)

    def _paged_repository_nodes(
        self,
        owner: str,
        repo: str,
        edge: str,
    ) -> tuple[str, ...]:
        cursor = ""
        nodes: list[str] = []
        while True:
            page = self.repository_nodes(owner, repo, edge, cursor)
            nodes.extend(page.nodes)
            if not page.has_next_page:
                break
            cursor = page.end_cursor
            if not cursor:
                raise DiscoveryError(f"missing cursor for repository edge: {edge}")
        return tuple(nodes)

    def _paged_owner_nodes(
        self,
        owner_ref: str,
        edge: str,
        owner_type: str,
    ) -> tuple[str, ...]:
        cursor = ""
        nodes: list[str] = []
        while True:
            page = self.owner_nodes(owner_ref, edge, cursor, owner_type)
            nodes.extend(page.nodes)
            if not page.has_next_page:
                break
            cursor = page.end_cursor
            if not cursor:
                raise DiscoveryError(f"missing cursor for owner edge: {edge}")
        return tuple(nodes)

    def _repository_collaborators(self, owner: str, repo: str) -> tuple[str, ...]:
        path = (
            f"repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
            "/collaborators?per_page=100"
        )
        collaborators: list[str] = []
        for response in self.client.rest_pages(path):
            response_value: object = response.value
            if not isinstance(response_value, list):
                raise DiscoveryError(
                    f"invalid collaborators response for {owner}/{repo}"
                )
            for node in cast(list[object], response_value):
                if not isinstance(node, dict):
                    continue
                value = cast(dict[str, object], node)
                login = value.get("login")
                owner_id = _positive_id(value.get("id"))
                if not isinstance(login, str) or not login or owner_id is None:
                    continue
                identity = OwnerIdentity(str(owner_id), login)
                self.cache.cache(identity.ref)
                collaborators.append(identity.ref)
        return tuple(collaborators)

    def _resolve_logins(self, logins: Iterable[str]) -> tuple[str, ...]:
        resolved: list[str] = []
        for login in _unique([value for value in logins if value]):
            result = self.resolve_owner(login)
            if result.owner_ref is not None:
                resolved.append(result.owner_ref)
        return tuple(resolved)

    def _connection_page(
        self,
        connection: dict[str, object],
        *,
        edge: str,
    ) -> DiscoveryPage:
        nodes = connection.get("nodes")
        page_info = _page_info(connection.get("pageInfo"))
        if not isinstance(nodes, list):
            return DiscoveryPage((), *page_info)

        logins: list[str] = []
        for node in cast(list[object], nodes):
            identity = _discovery_node_identity(node, edge=edge)
            if identity is not None:
                self.cache.cache(identity.ref)
                logins.append(identity.login)
                continue
            login = _discovery_node_login(node, edge=edge)
            if login:
                logins.append(login)
        return DiscoveryPage(tuple(logins), *page_info)


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _chunks(values: Sequence[str], size: int) -> Generator[list[str], None, None]:
    for index in range(0, len(values), size):
        yield list(values[index : index + size])


def _graphql_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _owner_lookup_query(owners: Sequence[str]) -> str:
    selections = " ".join(
        f"o{index}: repositoryOwner(login:{_graphql_string(owner)}) "
        "{ login ... on User { databaseId } ... on Organization { databaseId } }"
        for index, owner in enumerate(owners)
    )
    return f"query {{ {selections} }}"


def _owner_type_query(owner: str) -> str:
    return (
        "query { owner: repositoryOwner(login:"
        f"{_graphql_string(owner)}) {{ __typename }} }}"
    )


def _after_arg(cursor: str) -> str:
    return f", after:{_graphql_string(cursor)}" if cursor else ""


def _repository_parts(node: str) -> tuple[str, str]:
    if "/" not in node:
        return "", ""
    owner, repo = node.split("/", maxsplit=1)
    return owner, repo


def _without_self(values: Iterable[str], owner: str) -> tuple[str, ...]:
    return tuple(value for value in values if owner not in value)


def _repository_discovery_query(
    owner: str,
    repo: str,
    edge: str,
    cursor: str,
) -> str:
    after = _after_arg(cursor)
    if edge in {"stargazers", "watchers"}:
        return (
            "query { repository(owner:"
            f"{_graphql_string(owner)}, name:{_graphql_string(repo)}) "
            f"{{ {edge}(first:100{after}) "
            "{ nodes { login databaseId } "
            "pageInfo { hasNextPage endCursor } } } }"
        )
    if edge == "forks":
        return (
            "query { repository(owner:"
            f"{_graphql_string(owner)}, name:{_graphql_string(repo)}) "
            "{ forks(first:100"
            f"{after}) {{ nodes {{ owner {{ login ... on User {{ databaseId }} "
            "... on Organization { databaseId } } } } "
            "pageInfo { hasNextPage endCursor } } } }"
        )
    raise DiscoveryError(f"unsupported repository discovery edge: {edge}")


def _owner_discovery_query(
    owner: str,
    owner_type: str,
    edge: str,
    cursor: str,
) -> str:
    after = _after_arg(cursor)
    if edge in {"followers", "following", "organizations"}:
        return (
            "query { owner: repositoryOwner(login:"
            f"{_graphql_string(owner)}) {{ ... on User {{ {edge}(first:100{after}) "
            "{ nodes { login databaseId } "
            "pageInfo { hasNextPage endCursor } } } } }"
        )
    if edge == "people":
        return (
            "query { owner: repositoryOwner(login:"
            f"{_graphql_string(owner)}) {{ ... on {owner_type} "
            "{ membersWithRole(first:100"
            f"{after}) {{ nodes {{ login databaseId }} "
            "pageInfo { hasNextPage endCursor } } } } }"
        )
    raise DiscoveryError(f"unsupported owner discovery edge: {edge}")


def _data_value(value: object, key: str) -> object | None:
    if not isinstance(value, dict):
        return None
    data = cast(dict[str, object], value).get("data")
    if not isinstance(data, dict):
        return None
    return cast(dict[str, object], data).get(key)


def _owner_lookup_identities(
    value: object,
    owners: Sequence[str],
) -> list[OwnerIdentity | None] | None:
    if not isinstance(value, dict):
        return None
    data = cast(dict[str, object], value).get("data")
    if not isinstance(data, dict):
        return None

    identities: list[OwnerIdentity | None] = []
    for index in range(len(owners)):
        alias = f"o{index}"
        if alias not in data:
            return None
        node = cast(dict[str, object], data).get(alias)
        if node is None:
            identities.append(None)
            continue
        if not isinstance(node, dict):
            return None
        identity = _node_owner_identity(cast(dict[str, object], node))
        identities.append(identity)
    return identities


def _node_owner_identity(node: dict[str, object]) -> OwnerIdentity | None:
    login = node.get("login")
    owner_id = _positive_id(node.get("databaseId"))
    if not isinstance(login, str) or not login or owner_id is None:
        return None
    return OwnerIdentity(str(owner_id), login)


def _discovery_node_identity(node: object, *, edge: str) -> OwnerIdentity | None:
    if not isinstance(node, dict):
        return None
    value = cast(dict[str, object], node)
    if edge == "forks":
        owner = value.get("owner")
        return (
            _node_owner_identity(cast(dict[str, object], owner))
            if isinstance(owner, dict)
            else None
        )
    return _node_owner_identity(value)


def _discovery_node_login(node: object, *, edge: str) -> str | None:
    if not isinstance(node, dict):
        return None
    value = cast(dict[str, object], node)
    if edge == "forks":
        owner = value.get("owner")
        if not isinstance(owner, dict):
            return None
        login = cast(dict[str, object], owner).get("login")
    else:
        login = value.get("login")
    return login if isinstance(login, str) and login else None


def _page_info(value: object) -> tuple[bool, str]:
    if not isinstance(value, dict):
        return False, ""
    page_info = cast(dict[str, object], value)
    cursor = page_info.get("endCursor")
    return (
        page_info.get("hasNextPage") is True,
        cursor if isinstance(cursor, str) else "",
    )


def _rest_owner_identity(value: object, *, fallback_login: str) -> OwnerIdentity | None:
    if not isinstance(value, dict):
        return None
    data = cast(dict[str, Any], value)
    owner_id = _positive_id(data.get("id"))
    if owner_id is None:
        return None
    login = data.get("login")
    if not isinstance(login, str) or not login:
        login = fallback_login
    return OwnerIdentity(str(owner_id), login)


def _positive_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
