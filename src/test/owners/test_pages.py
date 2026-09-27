"""Tests for durable admission of global account pages."""

from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from bkg_py.discovery import DiscoveryError, OwnerIdentityCache, OwnerIdentityResolver
from bkg_py.github import GitHubClient, GitHubSettings
from bkg_py.locking import FileLockOptions
from bkg_py.owners.pages import OwnerPageAdmissionConfig, admit_owner_page
from bkg_py.runtime import GracefulStop
from bkg_py.state import StateStore


def _client(handler: httpx.MockTransport) -> GitHubClient:
    return GitHubClient(
        GitHubSettings(token="", user_agent="test-agent"),
        client=httpx.Client(transport=handler),
    )


def test_owner_page_admitter_queues_new_rest_owners_and_advances_marker(
    tmp_path: Path,
) -> None:
    """Only new owners are queued before committing the response cursor."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {
            "per_page": "2",
            "since": "0",
        }
        if request.url.path == "/users":
            return httpx.Response(
                200,
                json=[
                    {"id": 1, "login": "alpha"},
                    {"id": 2, "login": "beta"},
                ],
                headers={"Link": '<https://api.github.com/users?since=2>; rel="next"'},
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
            lock_options=FileLockOptions(poll_interval=0),
        ),
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
            "since": "0",
        }
        if request.url.path == "/users":
            return httpx.Response(200, json=[{"id": 2, "login": "beta"}])
        pytest.fail(f"unexpected request: {request.url}")
        raise AssertionError

    state = StateStore(tmp_path / ".env", lock_poll_interval=0)
    owners = tmp_path / "owners.txt"
    owners.write_text("2/BETA\n", encoding="utf-8")
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
            lock_options=FileLockOptions(poll_interval=0),
        ),
        100,
    )

    assert result.admitted_count == 0
    assert result.owners_count == 1
    assert not result.has_more
    assert result.requested_logins == ("beta",)
    assert owners.read_text(encoding="utf-8") == "2/BETA\n"
    assert state.get_int("BKG_LAST_SCANNED_ID") == 2


def test_owner_page_admitter_advances_marker_for_known_package_owner(
    tmp_path: Path,
) -> None:
    """Already indexed package owners still advance the REST since marker."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {
            "per_page": "100",
            "since": "0",
        }
        if request.url.path == "/users":
            return httpx.Response(200, json=[{"id": 7, "login": "indexed"}])
        pytest.fail(f"unexpected request: {request.url}")
        raise AssertionError

    state = StateStore(tmp_path / ".env", lock_poll_interval=0)
    owners = tmp_path / "owners.txt"
    packages_all = tmp_path / "packages_all"
    packages_all.write_text(
        "container|Indexed|repo|package|2026-06-18\n",
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
            lock_options=FileLockOptions(poll_interval=0),
        ),
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
            "since": "0",
        }
        if request.url.path == "/users":
            return httpx.Response(200, json=[{"id": 9, "login": "full"}])
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
            owner_file_max_bytes=1,
            lock_options=FileLockOptions(poll_interval=0),
        ),
        100,
    )

    assert result.admitted_count == 0
    assert not result.requested_logins
    assert owners.read_text(encoding="utf-8") == ""
    assert state.get_int("BKG_LAST_SCANNED_ID") == 0


def test_owner_pages_resume_from_committed_cursor(tmp_path: Path) -> None:
    """A short linked page continues; a full unlinked page ends the traversal."""

    cursors: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/users"
        assert set(request.url.params) == {"since", "per_page"}
        cursor = request.url.params["since"]
        cursors.append(cursor)
        if cursor == "7":
            return httpx.Response(
                200,
                json=[{"id": 10, "login": "alpha"}],
                headers={"Link": '<https://api.github.com/users?since=20>; rel="next"'},
            )
        if cursor == "20":
            return httpx.Response(
                200,
                json=[{"id": 30, "login": "beta"}, {"id": 40, "login": "gamma"}],
            )
        assert cursor == "40"
        return httpx.Response(200, json=[])

    state = StateStore(tmp_path / ".env", lock_poll_interval=0)
    state.set("BKG_LAST_SCANNED_ID", 7)
    config = OwnerPageAdmissionConfig(
        state, tmp_path / "owners.txt", tmp_path / "packages_all"
    )
    resolver = OwnerIdentityResolver(
        OwnerIdentityCache(tmp_path / "cache"), _client(httpx.MockTransport(respond))
    )

    assert admit_owner_page(resolver, config, 2).has_more
    assert state.get_int("BKG_LAST_SCANNED_ID") == 20
    resumed = replace(config, state=StateStore(state.path, lock_poll_interval=0))
    assert not admit_owner_page(resolver, resumed, 2).has_more
    assert state.get_int("BKG_LAST_SCANNED_ID") == 40
    assert not admit_owner_page(resolver, resumed, 2).has_more
    assert state.get_int("BKG_LAST_SCANNED_ID") == 40
    assert cursors == ["7", "20", "40"]
    assert config.owners_path.read_text(encoding="utf-8").splitlines() == [
        "10/alpha",
        "30/beta",
        "40/gamma",
    ]


def test_capped_owner_page_replays_without_skipping_later_known_owner(
    tmp_path: Path,
) -> None:
    """A partially admitted page keeps its cursor and deduplicates on retry."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.params["since"] == "7"
        return httpx.Response(
            200,
            json=[
                {"id": 10, "login": "alpha"},
                {"id": 20, "login": "beta"},
                {"id": 30, "login": "indexed"},
            ],
            headers={"Link": '<https://api.github.com/users?since=40>; rel="next"'},
        )

    state = StateStore(tmp_path / ".env", lock_poll_interval=0)
    state.set("BKG_LAST_SCANNED_ID", 7)
    packages = tmp_path / "packages_all"
    packages.write_text("container|indexed|repo|package|2026-06-18\n", encoding="utf-8")
    config = OwnerPageAdmissionConfig(
        state, tmp_path / "owners.txt", packages, owner_file_max_bytes=len("10/alpha\n")
    )
    resolver = OwnerIdentityResolver(
        OwnerIdentityCache(tmp_path / "cache"), _client(httpx.MockTransport(respond))
    )

    result = admit_owner_page(resolver, config, 100)
    assert result.admitted_count == 1
    assert not result.has_more
    assert state.get_int("BKG_LAST_SCANNED_ID") == 7
    assert config.owners_path.read_text(encoding="utf-8") == "10/alpha\n"

    result = admit_owner_page(resolver, replace(config, owner_file_max_bytes=100), 100)
    assert result.admitted_count == 1
    assert result.has_more
    assert state.get_int("BKG_LAST_SCANNED_ID") == 40
    assert config.owners_path.read_text(encoding="utf-8") == "10/alpha\n20/beta\n"


def test_owner_page_replays_after_interrupted_cursor_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interruption between queue and cursor writes cannot lose owners."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.params["since"] == "0"
        return httpx.Response(200, json=[{"id": 10, "login": "alpha"}])

    def interrupt_commit(_self: StateStore, _key: str, _value: object) -> None:
        raise GracefulStop("interrupted cursor write")

    state = StateStore(tmp_path / ".env", lock_poll_interval=0)
    config = OwnerPageAdmissionConfig(
        state, tmp_path / "owners.txt", tmp_path / "packages_all"
    )
    resolver = OwnerIdentityResolver(
        OwnerIdentityCache(tmp_path / "cache"), _client(httpx.MockTransport(respond))
    )
    with monkeypatch.context() as patch:
        patch.setattr(StateStore, "set", interrupt_commit)
        with pytest.raises(GracefulStop, match="interrupted cursor write"):
            admit_owner_page(resolver, config, 100)

    assert state.get_int("BKG_LAST_SCANNED_ID") == 0
    assert config.owners_path.read_text(encoding="utf-8") == "10/alpha\n"
    assert admit_owner_page(resolver, config, 100).admitted_count == 0
    assert state.get_int("BKG_LAST_SCANNED_ID") == 10
    assert config.owners_path.read_text(encoding="utf-8") == "10/alpha\n"


def test_owner_page_preserves_unterminated_manual_request(tmp_path: Path) -> None:
    """Appending an account must not merge it into the preceding manual login."""

    resolver = OwnerIdentityResolver(
        OwnerIdentityCache(tmp_path / "cache"),
        _client(
            httpx.MockTransport(
                lambda _request: httpx.Response(
                    200, json=[{"id": 10, "login": "alpha"}]
                )
            )
        ),
    )
    state = StateStore(tmp_path / ".env", lock_poll_interval=0)
    owners = tmp_path / "owners.txt"
    owners.write_text("existing", encoding="utf-8")
    expected = "existing\n10/alpha\n"
    config = OwnerPageAdmissionConfig(
        state, owners, tmp_path / "packages_all", owner_file_max_bytes=len(expected)
    )

    assert admit_owner_page(resolver, config, 100).admitted_count == 1
    assert owners.read_text(encoding="utf-8") == expected
    assert state.get_int("BKG_LAST_SCANNED_ID") == 10


@pytest.mark.parametrize(
    ("payload", "query"),
    [
        ({"message": "unexpected shape"}, "since=20"),
        ([{"id": 10, "login": "alpha"}, {"id": 11}], "since=20"),
        ([{"id": 10, "login": "alpha"}, None], "since=20"),
        ([{"id": 10, "login": "alpha"}], "since=7"),
        ([{"id": 10, "login": "alpha"}], "since=20&since=30"),
        ([{"id": 10, "login": "alpha"}], "page=2"),
        ([], "since=20"),
    ],
)
def test_invalid_owner_pages_leave_queue_and_cursor_unchanged(
    tmp_path: Path, payload: object, query: str
) -> None:
    """Malformed records or continuation links cannot silently skip accounts."""

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=payload,
            headers={"Link": f'<https://api.github.com/users?{query}>; rel="next"'},
        )

    state = StateStore(tmp_path / ".env", lock_poll_interval=0)
    state.set("BKG_LAST_SCANNED_ID", 7)
    config = OwnerPageAdmissionConfig(
        state, tmp_path / "owners.txt", tmp_path / "packages_all"
    )
    resolver = OwnerIdentityResolver(
        OwnerIdentityCache(tmp_path / "cache"), _client(httpx.MockTransport(respond))
    )

    with pytest.raises(DiscoveryError, match="invalid REST owner"):
        admit_owner_page(resolver, config, 100)

    assert state.get_int("BKG_LAST_SCANNED_ID") == 7
    assert not config.owners_path.exists()
