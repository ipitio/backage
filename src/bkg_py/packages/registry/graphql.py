"""GitHub GraphQL package-file sizing for Maven and Gradle artifacts."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.parse import unquote

from ...github import (
    GitHubError,
    GitHubGraphQLError,
    GitHubJsonResponse,
    GitHubResponseError,
)
from ..enrichment import (
    DeduplicatedDiagnostics,
    RequestCircuit,
    transient_request_error,
)
from .artifacts import (
    ArtifactSizeRequest,
    ArtifactSizeResult,
    ArtifactSizeSemantics,
    SingleFlightCache,
)

DiagnosticSink = Callable[[str], None]
_FILE_PAGE_SIZE = 100
_MAX_FILE_PAGES = 10
_SIDECAR_SUFFIXES = (
    ".asc",
    ".md5",
    ".sha",
    ".sha1",
    ".sha224",
    ".sha256",
    ".sha384",
    ".sha512",
    ".sig",
    ".sign",
)


def _ignore_diagnostic(_message: str) -> None:
    pass


class GraphQLPackageClient(Protocol):  # pylint: disable=too-few-public-methods
    """GraphQL operation required by package-file size adapters."""

    def graphql(self, query: str) -> GitHubJsonResponse:
        """Execute one authenticated GitHub GraphQL query."""

        raise NotImplementedError


@dataclass(frozen=True)
class _PackageFile:
    name: str
    size: object
    url: object


@dataclass(frozen=True)
class _PackageFilePage:
    files: tuple[_PackageFile, ...]
    next_cursor: str | None
    version_missing: bool = False


class _GraphQLCircuitUnavailable(RuntimeError):
    """The Maven package-file circuit is suppressing requests."""


class _MalformedPackageFileData(ValueError):
    """GitHub returned an unexpected package-file response shape."""


class MavenArtifactSizeAdapter:  # pylint: disable=too-few-public-methods
    """Sum downloadable Maven and Gradle files reported by GitHub GraphQL."""

    def __init__(
        self,
        client: GraphQLPackageClient,
        circuit: RequestCircuit,
        *,
        diagnostic: DiagnosticSink = _ignore_diagnostic,
    ) -> None:
        self.client = client
        self.circuit = circuit
        self.diagnostics = DeduplicatedDiagnostics(diagnostic)
        self._packages = SingleFlightCache[tuple[str, str, str], str | None](
            cache_error=_cache_permanent_github_error
        )
        self._versions = SingleFlightCache[tuple[str, str, str, str], int | None](
            cache_error=_cache_permanent_github_error
        )

    def resolve(self, request: ArtifactSizeRequest) -> ArtifactSizeResult:
        """Resolve one exact package version without downloading its files."""

        owner = request.context.owner
        repository = unquote(request.context.repo)
        package = unquote(request.context.package)
        identity = _request_identity(request)
        key = (
            owner.casefold(),
            repository.casefold(),
            package,
            request.version_name,
        )
        try:
            size = self._versions.get(
                key,
                lambda: self._load_version_size(
                    request,
                    owner,
                    repository,
                    package,
                ),
            )
        except _GraphQLCircuitUnavailable:
            return ArtifactSizeResult.unknown("maven-graphql-circuit-open")
        except GitHubError as error:
            self._report_request_error(error, identity)
            return ArtifactSizeResult.unknown("maven-graphql-request-failed")

        if size is None:
            return ArtifactSizeResult.unknown("maven-files-unavailable")
        return ArtifactSizeResult(
            size,
            ArtifactSizeSemantics.COMPRESSED_DOWNLOAD,
            "github-maven-package-files",
        )

    def _load_version_size(
        self,
        request: ArtifactSizeRequest,
        owner: str,
        repository: str,
        package: str,
    ) -> int | None:
        package_id = self._packages.get(
            (owner.casefold(), repository.casefold(), package),
            lambda: self._load_package_id(request, owner, repository, package),
        )
        if package_id is None:
            return None

        identity = _request_identity(request)
        cursor = ""
        total = 0
        included = 0
        for _page_number in range(1, _MAX_FILE_PAGES + 1):
            page = self._load_file_page(
                package_id,
                request.version_name,
                cursor,
                identity,
            )
            if page is None or page.version_missing:
                return None
            page_total = self._page_total(page, identity)
            if page_total is None:
                return None
            total += page_total[0]
            included += page_total[1]
            if page.next_cursor is None:
                if included == 0:
                    self._metadata_problem(
                        "no-downloads",
                        identity,
                        "contained no downloadable payload files",
                    )
                    return None
                return total
            cursor = page.next_cursor
        self._metadata_problem(
            "page-limit",
            identity,
            f"exceeded the {_MAX_FILE_PAGES}-page file limit",
        )
        return None

    def _load_file_page(
        self,
        package_id: str,
        version: str,
        cursor: str,
        identity: str,
    ) -> _PackageFilePage | None:
        response = self._remote(_version_files_query(package_id, version, cursor))
        page = _package_file_page(response.value, version)
        if page is None:
            self._metadata_problem(
                "files-shape",
                identity,
                "returned malformed version-file data",
            )
        return page

    def _page_total(
        self,
        page: _PackageFilePage,
        identity: str,
    ) -> tuple[int, int] | None:
        total = 0
        included = 0
        for package_file in page.files:
            if _is_sidecar(package_file.name):
                continue
            if not isinstance(package_file.url, str) or not package_file.url:
                continue
            size = package_file.size
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                self._metadata_problem(
                    "file-size",
                    identity,
                    "contained a downloadable file without a valid size",
                )
                return None
            total += size
            included += 1
        return total, included

    def _load_package_id(
        self,
        request: ArtifactSizeRequest,
        owner: str,
        repository: str,
        package: str,
    ) -> str | None:
        response = self._remote(_package_query(owner, repository, package))
        package_id = _repository_package_id(
            response.value,
            owner,
            repository,
            package,
        )
        if package_id is None:
            self._metadata_problem(
                "package-missing",
                _request_identity(request),
                "did not identify one matching repository package",
            )
        return package_id

    def _remote(self, query: str) -> GitHubJsonResponse:
        with self.circuit.request("maven") as lease:
            if not lease:
                raise _GraphQLCircuitUnavailable
            try:
                response = self.client.graphql(query)
            except GitHubError as error:
                if _transient_graphql_error(error):
                    cooldown = lease.record_transient_failure()
                    if cooldown is not None:
                        self.diagnostics.report(
                            f"cooldown:{cooldown}",
                            "maven GraphQL artifact-size requests paused for "
                            f"{cooldown:.0f}s after repeated failures",
                        )
                else:
                    lease.record_success()
                raise
            lease.record_success()
            return response

    def _metadata_problem(self, key: str, identity: str, detail: str) -> None:
        self.diagnostics.report(
            f"metadata:{key}",
            f"maven GraphQL artifact metadata {detail}; leaving size unknown "
            f"(example: {identity})",
        )

    def _report_request_error(self, error: GitHubError, identity: str) -> None:
        if _transient_graphql_error(error):
            return
        status = error.status_code if isinstance(error, GitHubResponseError) else None
        category = f"HTTP {status}" if status is not None else type(error).__name__
        self.diagnostics.report(
            f"request:{category}",
            f"maven GraphQL artifact sizing received {category}; leaving size "
            f"unknown (example: {identity})",
        )


def _package_query(owner: str, repository: str, package: str) -> str:
    return (
        "query { repository(owner:"
        f"{_graphql_string(owner)}, name:{_graphql_string(repository)}) "
        "{ packages(first:2, names:["
        f"{_graphql_string(package)}], packageType:MAVEN) "
        "{ nodes { id name repository { nameWithOwner } } } } }"
    )


def _version_files_query(package_id: str, version: str, cursor: str) -> str:
    after = f", after:{_graphql_string(cursor)}" if cursor else ""
    return (
        f"query {{ node(id:{_graphql_string(package_id)}) "
        "{ ... on Package { version(version:"
        f"{_graphql_string(version)}) {{ version files(first:{_FILE_PAGE_SIZE}"
        f"{after}) {{ nodes {{ name size url }} "
        "pageInfo { hasNextPage endCursor } } } } } }"
    )


def _repository_package_id(
    value: object,
    owner: str,
    repository: str,
    package: str,
) -> str | None:
    repository_value = _data_object(value, "repository")
    packages = _object_value(repository_value, "packages")
    nodes = _list_value(packages, "nodes")
    if nodes is None:
        return None
    expected_repository = f"{owner}/{repository}".casefold()
    matches: list[str] = []
    for raw_node in nodes:
        if not isinstance(raw_node, dict):
            continue
        node = cast(dict[str, object], raw_node)
        node_repository = _object_value(node, "repository")
        name_with_owner = (
            node_repository.get("nameWithOwner")
            if node_repository is not None
            else None
        )
        package_id = node.get("id")
        if (
            node.get("name") == package
            and isinstance(package_id, str)
            and package_id
            and isinstance(name_with_owner, str)
            and name_with_owner.casefold() == expected_repository
        ):
            matches.append(package_id)
    return matches[0] if len(matches) == 1 else None


def _package_file_page(
    value: object,
    expected_version: str,
) -> _PackageFilePage | None:
    try:
        node = _required_data_object(value, "node")
        raw_version = node.get("version")
        if raw_version is None:
            return _PackageFilePage((), None, version_missing=True)
        version = _required_object(raw_version)
        if version.get("version") != expected_version:
            raise _MalformedPackageFileData
        files = _required_object_value(version, "files")
        nodes = _required_list_value(files, "nodes")
        page_info = _required_object_value(files, "pageInfo")
        package_files = _package_files(nodes)
        next_cursor = _file_page_cursor(page_info)
    except _MalformedPackageFileData:
        return None
    return _PackageFilePage(package_files, next_cursor)


def _package_files(nodes: list[object]) -> tuple[_PackageFile, ...]:
    package_files: list[_PackageFile] = []
    for raw_file in nodes:
        file_value = _required_object(raw_file)
        name = file_value.get("name")
        if not isinstance(name, str) or not name:
            raise _MalformedPackageFileData
        package_files.append(
            _PackageFile(name, file_value.get("size"), file_value.get("url"))
        )
    return tuple(package_files)


def _file_page_cursor(page_info: dict[str, object]) -> str | None:
    has_next_page = page_info.get("hasNextPage")
    if not isinstance(has_next_page, bool):
        raise _MalformedPackageFileData
    end_cursor = page_info.get("endCursor")
    if has_next_page:
        if not isinstance(end_cursor, str) or not end_cursor:
            raise _MalformedPackageFileData
        return end_cursor
    return None


def _required_data_object(value: object, key: str) -> dict[str, object]:
    root = _required_object(value)
    data = _required_object_value(root, "data")
    return _required_object_value(data, key)


def _required_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _MalformedPackageFileData
    return cast(dict[str, object], value)


def _required_object_value(
    value: dict[str, object],
    key: str,
) -> dict[str, object]:
    return _required_object(value.get(key))


def _required_list_value(
    value: dict[str, object],
    key: str,
) -> list[object]:
    nested = value.get(key)
    if not isinstance(nested, list):
        raise _MalformedPackageFileData
    return cast(list[object], nested)


def _data_object(value: object, key: str) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    data = _object_value(cast(dict[str, object], value), "data")
    return _object_value(data, key)


def _object_value(
    value: dict[str, object] | None,
    key: str,
) -> dict[str, object] | None:
    if value is None:
        return None
    nested = value.get(key)
    return cast(dict[str, object], nested) if isinstance(nested, dict) else None


def _list_value(
    value: dict[str, object] | None,
    key: str,
) -> list[object] | None:
    if value is None:
        return None
    nested = value.get(key)
    return cast(list[object], nested) if isinstance(nested, list) else None


def _graphql_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _is_sidecar(name: str) -> bool:
    return name.casefold().endswith(_SIDECAR_SUFFIXES)


def _transient_graphql_error(error: GitHubError) -> bool:
    return isinstance(error, GitHubGraphQLError) or transient_request_error(error)


def _cache_permanent_github_error(error: Exception) -> bool:
    return isinstance(error, GitHubError) and not _transient_graphql_error(error)


def _request_identity(request: ArtifactSizeRequest) -> str:
    return (
        f"{request.context.owner}/{unquote(request.context.repo)}/"
        f"{unquote(request.context.package)}/{request.version_name}"
    )
