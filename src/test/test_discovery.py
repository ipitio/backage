"""Tests for owner identity discovery helpers."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from bkg_py.cli import main
from bkg_py.discovery import (
    OwnerIdentityCache,
    OwnerIdentityResolver,
)
from bkg_py.github import GitHubClient, GitHubSettings
from bkg_py.owners.pages import OwnerPageAdmissionConfig, admit_owner_page
from bkg_py.result import ExitStatus
from bkg_py.state import StateStore

TEST_TOKEN = "github_pat_discovery_secret"


def _settings(**overrides: object) -> GitHubSettings:
    values: dict[str, object] = {
        "token": TEST_TOKEN,
        "total_timeout": 30,
        "initial_backoff": 1,
        "max_backoff": 8,
    }
    values.update(overrides)
    return GitHubSettings(**values)  # type: ignore[arg-type]


def _client(
    handler: httpx.MockTransport,
    *,
    settings: GitHubSettings | None = None,
) -> GitHubClient:
    return GitHubClient(
        settings or _settings(),
        client=httpx.Client(transport=handler),
    )


def _assert_top_level_rate_limit(query: str) -> None:
    rate_limit_index = query.index("rateLimit")
    balance = 0
    for char in query[:rate_limit_index]:
        if char == "{":
            balance += 1
        elif char == "}":
            balance -= 1
        assert balance >= 0
    assert balance == 1

    for char in query[rate_limit_index:]:
        if char == "{":
            balance += 1
        elif char == "}":
            balance -= 1
        assert balance >= 0
    assert balance == 0


def test_cache_replaces_stale_ref_and_reports_conflicts(tmp_path: Path) -> None:
    """The owner ID cache preserves one unambiguous ref per login."""

    cache = OwnerIdentityCache(tmp_path / "owner-id-cache.txt")

    cache.cache("200/beta")
    cache.cache("300/gamma")
    cache.cache("201/beta")

    assert cache.lookup("beta") == "201/beta"
    assert cache.lookup("gamma") == "300/gamma"

    cache.path.write_text("200/beta\n201/beta\n", encoding="utf-8")

    assert cache.lookup("beta") is None


def test_fresh_owner_resolution_bypasses_a_stale_identity_cache(
    tmp_path: Path,
) -> None:
    """Alias cleanup verifies GitHub's current ID before deleting old rows."""

    cache = OwnerIdentityCache(tmp_path / "owner-id-cache.txt")
    cache.cache("100/Alpha")

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.github.com/users/Alpha"
        return httpx.Response(
            200,
            json={"id": 200, "login": "Alpha", "type": "User"},
        )

    resolver = OwnerIdentityResolver(cache, _client(httpx.MockTransport(respond)))

    assert resolver.resolve_owner("Alpha").owner_ref == "100/Alpha"
    assert resolver.resolve_owner_fresh("Alpha").owner_ref == "200/Alpha"
    assert cache.lookup("Alpha") == "200/Alpha"


def test_resolve_candidate_file_uses_cache_before_network(tmp_path: Path) -> None:
    """Cached owner refs are emitted without touching GitHub."""

    cache = OwnerIdentityCache(tmp_path / "owner-id-cache.txt")
    cache.cache("200/beta")
    cache.cache("300/gamma")
    candidates = tmp_path / "candidates"
    candidates.write_text("beta\ngamma\n", encoding="utf-8")

    def respond(_request: httpx.Request) -> httpx.Response:
        pytest.fail("cached owners should not perform network requests")

    resolver = OwnerIdentityResolver(cache, _client(httpx.MockTransport(respond)))

    assert resolver.resolve_candidate_file(candidates) == ["200/beta", "300/gamma"]


def test_resolve_candidate_file_batches_graphql_and_records_misses(
    tmp_path: Path,
) -> None:
    """GraphQL owner lookup resolves canonical logins and writes missing owners."""

    cache = OwnerIdentityCache(tmp_path / "owner-id-cache.txt")
    candidates = tmp_path / "candidates"
    missing = tmp_path / "missing"
    candidates.write_text("123/alpha\nbeta\n0/gamma\ndelta\n", encoding="utf-8")

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.github.com/graphql"
        payload = json.loads(request.content)
        query = payload["query"]
        assert 'repositoryOwner(login:"beta")' in query
        assert 'repositoryOwner(login:"gamma")' in query
        assert 'repositoryOwner(login:"delta")' in query
        return httpx.Response(
            200,
            json={
                "data": {
                    "o0": {"login": "Beta", "databaseId": 200},
                    "o1": {"login": "gamma", "databaseId": 300},
                    "o2": None,
                    "rateLimit": {
                        "cost": 1,
                        "remaining": 4999,
                        "resetAt": "2026-06-16T23:00:00Z",
                    },
                }
            },
        )

    resolver = OwnerIdentityResolver(cache, _client(httpx.MockTransport(respond)))

    assert resolver.resolve_candidate_file(candidates, missing_path=missing) == [
        "123/alpha",
        "200/Beta",
        "300/gamma",
    ]
    assert missing.read_text(encoding="utf-8") == "delta\n"
    assert "200/Beta" in cache.path.read_text(encoding="utf-8").splitlines()
    assert cache.lookup("beta") == "200/Beta"


def test_resolve_candidate_file_falls_back_after_graphql_failure(
    tmp_path: Path,
) -> None:
    """A failed GraphQL batch does not mark owners missing without REST proof."""

    cache = OwnerIdentityCache(tmp_path / "owner-id-cache.txt")
    candidates = tmp_path / "candidates"
    missing = tmp_path / "missing"
    candidates.write_text("fallback\n", encoding="utf-8")

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/graphql":
            return httpx.Response(502, text="try later", request=request)
        if request.url.path == "/users/fallback":
            return httpx.Response(200, json={"id": 400, "login": "fallback"})
        pytest.fail(f"unexpected request: {request.url}")
        raise AssertionError

    resolver = OwnerIdentityResolver(
        cache,
        _client(
            httpx.MockTransport(respond),
            settings=_settings(max_attempts=1),
        ),
    )

    assert resolver.resolve_candidate_file(candidates, missing_path=missing) == [
        "400/fallback"
    ]
    assert missing.read_text(encoding="utf-8") == ""


def test_resolve_owner_reports_authoritative_rest_miss(tmp_path: Path) -> None:
    """Missing REST user and organization responses are authoritative."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path in {"/users/missing", "/orgs/missing"}
        return httpx.Response(404, json={}, request=request)

    resolver = OwnerIdentityResolver(
        OwnerIdentityCache(tmp_path / "owner-id-cache.txt"),
        _client(httpx.MockTransport(respond)),
    )

    result = resolver.resolve_owner("missing")

    assert result.owner_ref is None
    assert result.missing


def test_owner_type_uses_graphql_typename(tmp_path: Path) -> None:
    """Owner type lookup returns GitHub's canonical GraphQL typename."""

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert 'repositoryOwner(login:"Lazztech")' in payload["query"]
        return httpx.Response(
            200,
            json={
                "data": {
                    "owner": {"__typename": "Organization"},
                    "rateLimit": {
                        "cost": 1,
                        "remaining": 4999,
                        "resetAt": "2026-06-16T23:00:00Z",
                    },
                }
            },
        )

    resolver = OwnerIdentityResolver(
        OwnerIdentityCache(tmp_path / "owner-id-cache.txt"),
        _client(httpx.MockTransport(respond)),
    )

    assert resolver.owner_type("Lazztech") == "Organization"


def test_repository_nodes_parses_stargazer_page_and_caches_ids(
    tmp_path: Path,
) -> None:
    """Repository discovery pages expose logins, cursors, and owner IDs."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/graphql"
        payload = json.loads(request.content)
        query = payload["query"]
        assert 'repository(owner:"ipitio", name:"backage")' in query
        assert 'stargazers(first:100, after:"cursor-one")' in query
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "stargazers": {
                            "nodes": [
                                {"login": "Alpha", "databaseId": 100},
                                {"login": "fallback-only"},
                            ],
                            "pageInfo": {
                                "hasNextPage": True,
                                "endCursor": "cursor-two",
                            },
                        }
                    },
                    "rateLimit": {
                        "cost": 1,
                        "remaining": 4999,
                        "resetAt": "2026-06-16T23:00:00Z",
                    },
                }
            },
        )

    cache = OwnerIdentityCache(tmp_path / "owner-id-cache.txt")
    resolver = OwnerIdentityResolver(cache, _client(httpx.MockTransport(respond)))

    page = resolver.repository_nodes(
        "ipitio",
        "backage",
        "stargazers",
        "cursor-one",
    )

    assert page.nodes == ("Alpha", "fallback-only")
    assert page.has_next_page
    assert page.end_cursor == "cursor-two"
    assert cache.lookup("alpha") == "100/Alpha"


def test_repository_nodes_extracts_fork_owners(tmp_path: Path) -> None:
    """Fork discovery emits fork owner logins instead of repository names."""

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        query = payload["query"]
        assert "forks(first:100)" in query
        assert "owner { login" in query
        _assert_top_level_rate_limit(query)
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "forks": {
                            "nodes": [
                                {"owner": {"login": "forker", "databaseId": 101}},
                                {"owner": {"login": "name-only"}},
                            ],
                            "pageInfo": {
                                "hasNextPage": False,
                                "endCursor": None,
                            },
                        }
                    },
                    "rateLimit": {
                        "cost": 1,
                        "remaining": 4999,
                        "resetAt": "2026-06-16T23:00:00Z",
                    },
                }
            },
        )

    cache = OwnerIdentityCache(tmp_path / "owner-id-cache.txt")
    resolver = OwnerIdentityResolver(cache, _client(httpx.MockTransport(respond)))

    page = resolver.repository_nodes("ipitio", "backage", "forks")

    assert page.nodes == ("forker", "name-only")
    assert not page.has_next_page
    assert page.end_cursor == ""
    assert cache.lookup("forker") == "101/forker"


def test_organization_logins_ignore_blank_owner(tmp_path: Path) -> None:
    """Blank connection rows do not trigger owner-type lookups."""

    def respond(_request: httpx.Request) -> httpx.Response:
        pytest.fail("blank organization discovery should not perform network requests")

    cache = OwnerIdentityCache(tmp_path / "owner-id-cache.txt")
    resolver = OwnerIdentityResolver(cache, _client(httpx.MockTransport(respond)))

    assert not resolver.organization_logins("")


def test_owner_nodes_parses_user_connection(tmp_path: Path) -> None:
    """User discovery pages expose owner logins and pagination state."""

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        query = payload["query"]
        assert 'repositoryOwner(login:"ipitio")' in query
        assert 'followers(first:100, after:"cursor-one")' in query
        return httpx.Response(
            200,
            json={
                "data": {
                    "owner": {
                        "followers": {
                            "nodes": [{"login": "follower", "databaseId": 102}],
                            "pageInfo": {
                                "hasNextPage": True,
                                "endCursor": "cursor-two",
                            },
                        }
                    },
                    "rateLimit": {
                        "cost": 1,
                        "remaining": 4999,
                        "resetAt": "2026-06-16T23:00:00Z",
                    },
                }
            },
        )

    cache = OwnerIdentityCache(tmp_path / "owner-id-cache.txt")
    resolver = OwnerIdentityResolver(cache, _client(httpx.MockTransport(respond)))

    page = resolver.owner_nodes("ipitio", "followers", "cursor-one", "User")

    assert page.nodes == ("follower",)
    assert page.has_next_page
    assert page.end_cursor == "cursor-two"
    assert cache.lookup("follower") == "102/follower"


def test_owner_nodes_parses_organization_members(tmp_path: Path) -> None:
    """Organization people discovery maps to the GraphQL members connection."""

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        query = payload["query"]
        assert 'repositoryOwner(login:"github")' in query
        assert "membersWithRole(first:100)" in query
        return httpx.Response(
            200,
            json={
                "data": {
                    "owner": {
                        "membersWithRole": {
                            "nodes": [{"login": "member", "databaseId": 103}],
                            "pageInfo": {
                                "hasNextPage": False,
                                "endCursor": None,
                            },
                        }
                    },
                    "rateLimit": {
                        "cost": 1,
                        "remaining": 4999,
                        "resetAt": "2026-06-16T23:00:00Z",
                    },
                }
            },
        )

    cache = OwnerIdentityCache(tmp_path / "owner-id-cache.txt")
    resolver = OwnerIdentityResolver(cache, _client(httpx.MockTransport(respond)))

    page = resolver.owner_nodes("github", "people", owner_type="Organization")

    assert page.nodes == ("member",)
    assert not page.has_next_page
    assert page.end_cursor == ""
    assert cache.lookup("member") == "103/member"


def test_owner_nodes_returns_empty_page_for_wrong_owner_type(
    tmp_path: Path,
) -> None:
    """Unsupported owner-type and edge pairings do not make GitHub requests."""

    def respond(_request: httpx.Request) -> httpx.Response:
        pytest.fail("wrong-type discovery should not perform network requests")

    resolver = OwnerIdentityResolver(
        OwnerIdentityCache(tmp_path / "owner-id-cache.txt"),
        _client(httpx.MockTransport(respond)),
    )

    assert not resolver.owner_nodes(
        "github",
        "followers",
        owner_type="Organization",
    ).nodes
    assert not resolver.owner_nodes("ipitio", "people", owner_type="User").nodes


def test_explore_repository_uses_graphql_and_rest_edges(tmp_path: Path) -> None:
    """Repository traversal spans GraphQL edges and REST collaborators."""

    requested: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/graphql":
            payload = json.loads(request.content)
            query = payload["query"]
            if "stargazers(first:100)" in query:
                nodes = [
                    {"login": "Alpha", "databaseId": 201},
                    {"login": "ipitio", "databaseId": 202},
                ]
                edge = "stargazers"
            elif "watchers(first:100)" in query:
                nodes = [{"login": "Watcher", "databaseId": 203}]
                edge = "watchers"
            elif "forks(first:100)" in query:
                nodes = [{"owner": {"login": "forker", "databaseId": 204}}]
                edge = "forks"
            else:
                pytest.fail(f"unexpected query: {query}")
                raise AssertionError
            return httpx.Response(
                200,
                json={
                    "data": {
                        "repository": {
                            edge: {
                                "nodes": nodes,
                                "pageInfo": {
                                    "hasNextPage": False,
                                    "endCursor": None,
                                },
                            }
                        },
                        "rateLimit": {
                            "cost": 1,
                            "remaining": 4999,
                            "resetAt": "2026-06-16T23:00:00Z",
                        },
                    }
                },
            )
        if request.url.path == "/repos/ipitio/backage/collaborators":
            return httpx.Response(
                200,
                json=[
                    {"login": "collab", "id": 205},
                    {"login": "missing-id"},
                    {"id": 206},
                ],
            )
        pytest.fail(f"unexpected request: {request.url}")
        raise AssertionError

    cache = OwnerIdentityCache(tmp_path / "owner-id-cache.txt")
    resolver = OwnerIdentityResolver(cache, _client(httpx.MockTransport(respond)))

    assert resolver.explore("ipitio/backage") == (
        "Alpha",
        "Watcher",
        "forker",
        "205/collab",
    )
    assert requested == [
        "/graphql",
        "/graphql",
        "/graphql",
        "/repos/ipitio/backage/collaborators",
    ]
    assert cache.lookup("collab") == "205/collab"


def test_explore_user_includes_organizations_once(tmp_path: Path) -> None:
    """User traversal expands organizations inside one resolver operation."""

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        query = payload["query"]
        if "__typename" in query:
            owner = {"__typename": "User"}
        elif "followers(first:100)" in query:
            owner = {
                "followers": {
                    "nodes": [{"login": "Follower", "databaseId": 301}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        elif "organizations(first:100)" in query:
            owner = {
                "organizations": {
                    "nodes": [{"login": "OrgOne", "databaseId": 302}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        else:
            pytest.fail(f"unexpected query: {query}")
            raise AssertionError
        return httpx.Response(
            200,
            json={
                "data": {
                    "owner": owner,
                    "rateLimit": {
                        "cost": 1,
                        "remaining": 4999,
                        "resetAt": "2026-06-16T23:00:00Z",
                    },
                }
            },
        )

    resolver = OwnerIdentityResolver(
        OwnerIdentityCache(tmp_path / "owner-id-cache.txt"),
        _client(httpx.MockTransport(respond)),
    )

    assert resolver.explore("ipitio", "followers") == ("Follower", "OrgOne")


def test_membership_returns_organization_members(tmp_path: Path) -> None:
    """Membership traversal uses integration-compatible REST pagination."""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/users/github":
            return httpx.Response(200, json={"type": "Organization"})
        assert request.url.path == "/orgs/github/members"
        assert dict(request.url.params) == {"per_page": "100", "page": "1"}
        return httpx.Response(200, json=[{"login": "member"}])

    resolver = OwnerIdentityResolver(
        OwnerIdentityCache(tmp_path / "owner-id-cache.txt"),
        _client(httpx.MockTransport(respond)),
    )

    assert resolver.membership("1/github") == ("member",)


def test_membership_returns_user_organizations(tmp_path: Path) -> None:
    """User deployments discover public organizations without GraphQL access."""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/users/ipitio":
            return httpx.Response(200, json={"type": "User"})
        assert request.url.path == "/users/ipitio/orgs"
        assert dict(request.url.params) == {"per_page": "100", "page": "1"}
        return httpx.Response(200, json=[{"login": "ExampleOrg"}])

    resolver = OwnerIdentityResolver(
        OwnerIdentityCache(tmp_path / "owner-id-cache.txt"),
        _client(httpx.MockTransport(respond)),
    )

    assert resolver.membership("2/ipitio") == ("ExampleOrg",)


def test_organization_logins_can_emit_resolved_refs(tmp_path: Path) -> None:
    """Resolved organization output reuses IDs cached from GraphQL pages."""

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        query = payload["query"]
        if "__typename" in query:
            owner = {"__typename": "User"}
        elif "organizations(first:100)" in query:
            owner = {
                "organizations": {
                    "nodes": [{"login": "OrgOne", "databaseId": 501}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        else:
            pytest.fail(f"unexpected query: {query}")
            raise AssertionError
        return httpx.Response(
            200,
            json={
                "data": {
                    "owner": owner,
                    "rateLimit": {
                        "cost": 1,
                        "remaining": 4999,
                        "resetAt": "2026-06-16T23:00:00Z",
                    },
                }
            },
        )

    resolver = OwnerIdentityResolver(
        OwnerIdentityCache(tmp_path / "owner-id-cache.txt"),
        _client(httpx.MockTransport(respond)),
    )

    assert resolver.organization_logins("ipitio", resolve=True) == ("501/OrgOne",)


def test_owner_page_merges_rest_user_and_organization_pages(
    tmp_path: Path,
) -> None:
    """REST owner page discovery preserves raw counts and deduplicates logins."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {
            "per_page": "100",
            "page": "2",
            "since": "7",
        }
        if request.url.path == "/users":
            return httpx.Response(
                200,
                json=[
                    {"id": 20, "login": "beta"},
                    {"id": 10, "login": "alpha"},
                ],
            )
        if request.url.path == "/organizations":
            return httpx.Response(
                200,
                json=[
                    {"id": 11, "login": "alpha"},
                    {"id": 30, "login": "gamma"},
                    {"id": 40},
                ],
            )
        pytest.fail(f"unexpected request: {request.url}")
        raise AssertionError

    resolver = OwnerIdentityResolver(
        OwnerIdentityCache(tmp_path / "owner-id-cache.txt"),
        _client(httpx.MockTransport(respond)),
    )

    page = resolver.owner_page(2, last_id=7, per_page=100)

    assert page.users_count == 2
    assert page.orgs_count == 3
    assert page.owners == (
        {"id": 10, "login": "alpha"},
        {"id": 20, "login": "beta"},
        {"id": 30, "login": "gamma"},
    )


def test_owner_page_admitter_queues_new_rest_owners_and_advances_marker(
    tmp_path: Path,
) -> None:
    """REST owner page admission mirrors the shell queue and marker behavior."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {
            "per_page": "2",
            "page": "1",
            "since": "0",
        }
        if request.url.path == "/users":
            return httpx.Response(
                200,
                json=[
                    {"id": 1, "login": "alpha"},
                    {"id": 2, "login": "beta"},
                ],
            )
        if request.url.path == "/organizations":
            return httpx.Response(
                200,
                json=[
                    {"id": 2, "login": "beta"},
                ],
            )
        pytest.fail(f"unexpected request: {request.url}")
        raise AssertionError

    cache = OwnerIdentityCache(tmp_path / "owner-id-cache.txt")
    state = StateStore(tmp_path / ".env", lock_poll_interval=0)
    owners = tmp_path / "owners.txt"
    packages_all = tmp_path / "packages_all"
    packages_all.write_text("pkg|alpha|repo|package|2026-06-18\n", encoding="utf-8")

    result = admit_owner_page(
        OwnerIdentityResolver(cache, _client(httpx.MockTransport(respond))),
        OwnerPageAdmissionConfig(
            state,
            owners,
            packages_all,
            lock_poll_interval=0,
        ),
        1,
        2,
    )

    assert result.admitted_count == 1
    assert result.owners_count == 2
    assert result.has_more
    assert result.requested_logins == ("beta",)
    assert owners.read_text(encoding="utf-8") == "2/beta\n"
    assert cache.lookup("alpha") == "1/alpha"
    assert cache.lookup("beta") == "2/beta"
    assert state.get_int("BKG_LAST_SCANNED_ID") == 2


def test_owner_page_admitter_advances_marker_for_existing_queue_entry(
    tmp_path: Path,
) -> None:
    """Already queued owners still advance the REST since marker."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {
            "per_page": "100",
            "page": "1",
            "since": "0",
        }
        if request.url.path == "/users":
            return httpx.Response(200, json=[{"id": 2, "login": "beta"}])
        if request.url.path == "/organizations":
            return httpx.Response(200, json=[])
        pytest.fail(f"unexpected request: {request.url}")
        raise AssertionError

    state = StateStore(tmp_path / ".env", lock_poll_interval=0)
    owners = tmp_path / "owners.txt"
    owners.write_text("2/beta\n", encoding="utf-8")
    packages_all = tmp_path / "packages_all"
    packages_all.write_text("", encoding="utf-8")

    result = admit_owner_page(
        OwnerIdentityResolver(
            OwnerIdentityCache(tmp_path / "owner-id-cache.txt"),
            _client(httpx.MockTransport(respond)),
        ),
        OwnerPageAdmissionConfig(
            state,
            owners,
            packages_all,
            lock_poll_interval=0,
        ),
        1,
        100,
    )

    assert result.admitted_count == 0
    assert result.owners_count == 1
    assert not result.has_more
    assert result.requested_logins == ("beta",)
    assert owners.read_text(encoding="utf-8") == "2/beta\n"
    assert state.get_int("BKG_LAST_SCANNED_ID") == 2


def test_owner_page_admitter_advances_marker_for_known_package_owner(
    tmp_path: Path,
) -> None:
    """Already indexed package owners still advance the REST since marker."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {
            "per_page": "100",
            "page": "1",
            "since": "0",
        }
        if request.url.path == "/users":
            return httpx.Response(200, json=[{"id": 7, "login": "indexed"}])
        if request.url.path == "/organizations":
            return httpx.Response(200, json=[])
        pytest.fail(f"unexpected request: {request.url}")
        raise AssertionError

    state = StateStore(tmp_path / ".env", lock_poll_interval=0)
    owners = tmp_path / "owners.txt"
    packages_all = tmp_path / "packages_all"
    packages_all.write_text(
        "container|indexed|repo|package|2026-06-18\n",
        encoding="utf-8",
    )

    result = admit_owner_page(
        OwnerIdentityResolver(
            OwnerIdentityCache(tmp_path / "owner-id-cache.txt"),
            _client(httpx.MockTransport(respond)),
        ),
        OwnerPageAdmissionConfig(
            state,
            owners,
            packages_all,
            lock_poll_interval=0,
        ),
        1,
        100,
    )

    assert result.admitted_count == 0
    assert not result.requested_logins
    assert owners.read_text(encoding="utf-8") == ""
    assert state.get_int("BKG_LAST_SCANNED_ID") == 7


def test_owner_page_admitter_keeps_marker_when_owner_file_is_capped(
    tmp_path: Path,
) -> None:
    """A full owner queue does not advance past an owner it failed to append."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {
            "per_page": "100",
            "page": "1",
            "since": "0",
        }
        if request.url.path == "/users":
            return httpx.Response(200, json=[{"id": 9, "login": "full"}])
        if request.url.path == "/organizations":
            return httpx.Response(200, json=[])
        pytest.fail(f"unexpected request: {request.url}")
        raise AssertionError

    state = StateStore(tmp_path / ".env", lock_poll_interval=0)
    owners = tmp_path / "owners.txt"
    packages_all = tmp_path / "packages_all"
    packages_all.write_text("", encoding="utf-8")

    result = admit_owner_page(
        OwnerIdentityResolver(
            OwnerIdentityCache(tmp_path / "owner-id-cache.txt"),
            _client(httpx.MockTransport(respond)),
        ),
        OwnerPageAdmissionConfig(
            state,
            owners,
            packages_all,
            lock_poll_interval=0,
            owner_file_max_bytes=1,
        ),
        1,
        100,
    )

    assert result.admitted_count == 0
    assert not result.requested_logins
    assert owners.read_text(encoding="utf-8") == ""
    assert state.get_int("BKG_LAST_SCANNED_ID") == 0


def test_discovery_cli_resolves_existing_owner_ref_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The shell-facing discovery CLI preserves ID/login refs directly."""

    monkeypatch.setenv("BKG_ROOT", str(tmp_path))
    monkeypatch.setenv("BKG_ENV", str(tmp_path / "env.env"))

    status = main(["discovery", "resolve-owner", "123/alpha"])

    assert status == ExitStatus.SUCCESS
    assert capsys.readouterr().out == "123/alpha\n"
