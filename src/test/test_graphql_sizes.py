"""Tests for Maven and Gradle package-file sizing through GitHub GraphQL."""

from __future__ import annotations

from collections.abc import Iterable

import httpx
import pytest

import bkg_py.graphql_sizes
from bkg_py.artifact_sizes import ArtifactSizeRequest, ArtifactSizeSemantics
from bkg_py.enrichment import RequestCircuit, RequestCircuitSettings
from bkg_py.github import GitHubGraphQLError, GitHubJsonResponse
from bkg_py.graphql_sizes import MavenArtifactSizeAdapter
from bkg_py.versions import VersionListingContext


class _FakeGraphQLClient:  # pylint: disable=too-few-public-methods
    def __init__(self, responses: Iterable[object | Exception]) -> None:
        self.responses = list(responses)
        self.queries: list[str] = []

    def graphql(self, query: str) -> GitHubJsonResponse:
        """Return the next configured GraphQL response."""

        self.queries.append(query)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return GitHubJsonResponse(response, httpx.Headers())


def _request(version: str) -> ArtifactSizeRequest:
    return ArtifactSizeRequest(
        VersionListingContext(
            owner_type="orgs",
            owner="gradle",
            repo="declarative-lsp",
            package_type="maven",
            package="org.gradle.declarative-lsp",
        ),
        version_id=version,
        version_name=version,
    )


def _package_response(package_id: str = "P_package") -> object:
    return {
        "data": {
            "repository": {
                "packages": {
                    "nodes": [
                        {
                            "id": package_id,
                            "name": "org.gradle.declarative-lsp",
                            "repository": {"nameWithOwner": "gradle/declarative-lsp"},
                        }
                    ]
                }
            }
        }
    }


def _file_response(
    version: str,
    files: list[dict[str, object]],
    *,
    next_cursor: str | None = None,
) -> object:
    return {
        "data": {
            "node": {
                "version": {
                    "version": version,
                    "files": {
                        "nodes": files,
                        "pageInfo": {
                            "hasNextPage": next_cursor is not None,
                            "endCursor": next_cursor,
                        },
                    },
                }
            }
        }
    }


def _file(
    name: str, size: object, url: object = "https://files.example/item"
) -> dict[str, object]:
    return {"name": name, "size": size, "url": url}


def test_maven_adapter_sums_payloads_across_pages_and_excludes_sidecars() -> None:
    """Archives, metadata, and classifiers count while signatures do not."""

    version = "0.0.1-SNAPSHOT"
    client = _FakeGraphQLClient(
        [
            _package_response(),
            _file_response(
                version,
                [
                    _file("artifact.pom", 448),
                    _file("artifact.pom.sha256", 64),
                    _file("artifact.jar.asc", 512),
                    _file("unavailable.module", 25, None),
                ],
                next_cursor="page:2",
            ),
            _file_response(
                version,
                [
                    _file("artifact.jar", 1_000),
                    _file("artifact-sources.jar", 200),
                    _file("artifact.module", 50),
                    _file("artifact.jar.md5", 32),
                ],
            ),
        ]
    )
    adapter = MavenArtifactSizeAdapter(client, RequestCircuit())

    result = adapter.resolve(_request(version))

    assert result.size == 1_698
    assert result.semantics is ArtifactSizeSemantics.COMPRESSED_DOWNLOAD
    assert result.source == "github-maven-package-files"
    assert "packageType:MAVEN" in client.queries[0]
    assert 'name:"declarative-lsp"' in client.queries[0]
    assert "files(first:100" in client.queries[1]
    assert 'after:"page:2"' in client.queries[2]


def test_maven_adapter_reuses_package_identity_across_versions() -> None:
    """One repository package lookup serves every selected package version."""

    client = _FakeGraphQLClient(
        [
            _package_response(),
            _file_response("1.0.0", [_file("artifact.jar", 100)]),
            _file_response("2.0.0", [_file("artifact.jar", 200)]),
        ]
    )
    adapter = MavenArtifactSizeAdapter(client, RequestCircuit())

    assert adapter.resolve(_request("1.0.0")).size == 100
    assert adapter.resolve(_request("2.0.0")).size == 200

    assert len(client.queries) == 3
    assert sum("packages(first:2" in query for query in client.queries) == 1


def test_maven_adapter_rejects_partial_total_for_invalid_file_size() -> None:
    """A downloadable file with no valid size prevents an undercounted result."""

    client = _FakeGraphQLClient(
        [
            _package_response(),
            _file_response(
                "1.0.0",
                [
                    _file("artifact.jar", 100),
                    _file("artifact.pom", None),
                ],
            ),
        ]
    )
    diagnostics: list[str] = []
    adapter = MavenArtifactSizeAdapter(
        client,
        RequestCircuit(),
        diagnostic=diagnostics.append,
    )

    assert adapter.resolve(_request("1.0.0")).size == -1
    assert "without a valid size" in diagnostics[0]


def test_missing_repository_package_is_cached() -> None:
    """A missing package lookup is not repeated for each selected version."""

    client = _FakeGraphQLClient([{"data": {"repository": {"packages": {"nodes": []}}}}])
    diagnostics: list[str] = []
    adapter = MavenArtifactSizeAdapter(
        client,
        RequestCircuit(),
        diagnostic=diagnostics.append,
    )

    assert adapter.resolve(_request("1.0.0")).size == -1
    assert adapter.resolve(_request("2.0.0")).size == -1

    assert len(client.queries) == 1
    assert len(diagnostics) == 1
    assert "matching repository package" in diagnostics[0]


def test_maven_adapter_refuses_partial_result_at_file_page_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """More files than the bounded traversal can inspect remain unknown."""

    monkeypatch.setattr(bkg_py.graphql_sizes, "_MAX_FILE_PAGES", 2)
    client = _FakeGraphQLClient(
        [
            _package_response(),
            _file_response("1.0.0", [_file("one.jar", 100)], next_cursor="two"),
            _file_response("1.0.0", [_file("two.jar", 200)], next_cursor="three"),
        ]
    )
    diagnostics: list[str] = []
    adapter = MavenArtifactSizeAdapter(
        client,
        RequestCircuit(),
        diagnostic=diagnostics.append,
    )

    assert adapter.resolve(_request("1.0.0")).size == -1
    assert len(client.queries) == 3
    assert "2-page file limit" in diagnostics[0]


def test_graphql_failures_open_only_the_maven_size_circuit() -> None:
    """Repeated GraphQL failures pause Maven enrichment without more requests."""

    client = _FakeGraphQLClient(
        [
            GitHubGraphQLError("temporary GraphQL failure"),
            GitHubGraphQLError("temporary GraphQL failure"),
        ]
    )
    diagnostics: list[str] = []
    adapter = MavenArtifactSizeAdapter(
        client,
        RequestCircuit(
            RequestCircuitSettings(
                failure_threshold=2,
                cooldown_seconds=300,
            )
        ),
        diagnostic=diagnostics.append,
    )

    for version in ("1.0.0", "2.0.0", "3.0.0"):
        assert adapter.resolve(_request(version)).size == -1

    assert len(client.queries) == 2
    assert diagnostics == [
        "maven GraphQL artifact-size requests paused for 300s after repeated failures"
    ]
