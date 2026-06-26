#!/bin/bash

# Test doubles are invoked indirectly by sourced production functions.
# shellcheck disable=SC1091,SC2034,SC2317

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
src_dir=${src_dir:?}
workdir=${workdir:?}

init_bkg_state() {
    init_bkg_runtime_state "$BKG_ENV"
    : >"$BKG_OWNERS"
    : >"$BKG_OPTOUT"
    mkdir -p "$BKG_INDEX_DIR"
}

setup_discovery_fixture() {
    BKG_ENV="$workdir/env.env"
    BKG_OWNERS="$workdir/owners.txt"
    BKG_OPTOUT="$workdir/optout.txt"
    BKG_INDEX_DIR="$workdir/index"
    connections="$workdir/connections.txt"
    owners_file="$workdir/manual-owners.txt"
    index_repo="$workdir/index-repo"

    rm -rf "$BKG_INDEX_DIR" "$index_repo"
    : >"$BKG_ENV"
    : >"$BKG_OWNERS"
    : >"$BKG_OPTOUT"
    : >"$owners_file"
    : >"$workdir/all_owners_in_db"
    : >"$workdir/owners_partially_updated"
    : >"$workdir/owners_stale"
    mkdir -p "$BKG_INDEX_DIR" "$index_repo"

    git -C "$index_repo" init -q
    git -C "$index_repo" config user.name test
    git -C "$index_repo" config user.email test@example.com
    echo README >"$index_repo/README.md"
    git -C "$index_repo" add README.md
    git -C "$index_repo" commit -qm init
}

test_discovered_second_hop_org_survives_owner_admission() {
    local admitted

    setup_discovery_fixture
    printf '%s\n' gianlazz Lazztech >"$connections"

    pushd "$workdir" >/dev/null
    admitted=$(bash "$src_dir/lib/get.sh" 0 "$connections" 20 ipitio "$owners_file" "$index_repo")
    popd >/dev/null

    grep -Fxq Lazztech <<<"$admitted" || fail "Expected discovered second-hop org to survive owner admission"
}

test_discovered_owner_admission_includes_all_candidates_below_cap() {
    local admitted_capped
    local many_connections="$workdir/many-connections.txt"

    setup_discovery_fixture
    seq -f 'owner%03g' 1 300 >"$many_connections"

    pushd "$workdir" >/dev/null
    admitted_capped=$(bash "$src_dir/lib/get.sh" 0 "$many_connections" 100 ipitio "$owners_file" "$index_repo")
    popd >/dev/null

    [ "$(wc -l <<<"$admitted_capped")" -eq 301 ] || fail "Expected discovered owner admission to include all candidates when the four-times-request_limit cap is not reached"
    grep -Fxq ipitio <<<"$admitted_capped" || fail "Expected current owner to still be eligible within the capped owner admission set"
}

test_save_owner_queues_resolved_owner_id() {
    setup_discovery_fixture
    init_bkg_state

    save_owner '556677/Lazztech' >/dev/null
    grep -Fxq '556677/Lazztech' <<<"$(get_BKG_set BKG_OWNERS_QUEUE)" || fail "Expected discovered org to be queued for owner updates"
}

test_page_package_enqueues_package() {
    setup_discovery_fixture
    init_bkg_state

    pushd "$workdir" >/dev/null
    : >packages_already_updated
    owner_id=556677
    owner=Lazztech
    owner_type=orgs
    fast_out=false

    curl() {
        cat <<'EOF'
<div>
  <a href="/orgs/Lazztech/packages/container/package/libre-closet">libre-closet</a>
  <a href="/Lazztech/Libre-Closet">Libre-Closet</a>
</div>
EOF
    }

    run_parallel() {
        local function_name=$1
        local items=$2

        while IFS= read -r item; do
            [ -n "$item" ] || continue
            "$function_name" "$item"
        done <<<"$items"
    }

    page_package 1 >/dev/null
    popd >/dev/null

    grep -Fxq 'container/Libre-Closet/libre-closet' <<<"$(get_BKG_set BKG_PACKAGES_Lazztech)" || fail "Expected queued package list to include Lazztech/libre-closet"
}

test_page_package_accepts_repositoryless_package() {
    setup_discovery_fixture
    init_bkg_state

    pushd "$workdir" >/dev/null
    : >packages_already_updated
    owner_id=556677
    owner=Lazztech
    owner_type=orgs
    fast_out=false

    curl() {
        cat <<'EOF'
<div>
  <a href="/orgs/Lazztech/packages/container/package/tools%2Fworker">tools/worker</a>
  <a href="/Lazztech">Lazztech</a>
</div>
EOF
    }

    run_parallel() {
        local function_name=$1
        local items=$2

        while IFS= read -r item; do
            [ -n "$item" ] || continue
            "$function_name" "$item"
        done <<<"$items"
    }

    page_package 1 >/dev/null
    popd >/dev/null

    grep -Fxq 'container/tools%2Fworker/tools%2Fworker' <<<"$(get_BKG_set BKG_PACKAGES_Lazztech)" || fail "Expected repositoryless package to use its package slug as the repo bucket"
}

test_page_package_distinguishes_transport_failure_from_empty_listing() {
    local status

    setup_discovery_fixture
    init_bkg_state

    pushd "$workdir" >/dev/null
    owner_id=556677
    owner=Lazztech
    owner_type=orgs

    curl() {
        return 1
    }

    set +e
    page_package 1 >/dev/null
    status=$?
    set -e
    popd >/dev/null

    [ "$status" -eq 1 ] || fail "Expected failed owner listing transport to remain non-authoritative"
}

test_partial_owner_refresh_uses_known_package_identity() {
    local captured="$workdir/refreshed-package"
    local today
    local yesterday

    setup_discovery_fixture
    init_bkg_state
    BKG_INDEX_DB="$workdir/partial-owner.db"
    today=$(date -u +%Y-%m-%d)
    yesterday=$(date -u -d yesterday +%Y-%m-%d)
    BKG_BATCH_FIRST_STARTED=$today
    set_BKG BKG_BATCH_FIRST_STARTED "$today"
    set_BKG BKG_BATCH_MARKER test-batch
    sqlite_ensure_index_schema >/dev/null
    command sqlite3 "$BKG_INDEX_DB" "
        insert into '$BKG_INDEX_TBL_PKG' (
            owner_id, owner_type, package_type, owner, repo, package,
            downloads, downloads_month, downloads_week, downloads_day,
            size, date
        ) values
            ('556677', 'orgs', 'container', 'KnownOwner', 'FreshRepo', 'fresh', 1, 1, 1, 1, 1, '$today'),
            ('556677', 'orgs', 'container', 'KnownOwner', 'StaleRepo', 'stale', 1, 1, 1, 1, 1, '$yesterday');
    "

    pushd "$workdir" >/dev/null
    printf '%s\n' "556677|KnownOwner|FreshRepo|fresh|$today" >packages_already_updated

    curl() {
        printf '%s\n' '<a href="/orgs/KnownOwner/people">people</a>'
    }

    run_parallel() {
        local function_name=$1
        local items=$2
        local item

        while IFS= read -r item; do
            [ -n "$item" ] || continue
            "$function_name" "$item"
        done <<<"$items"
    }

    update_package() {
        printf '%s\n' "$1" >"$captured"
        command sqlite3 "$BKG_INDEX_DB" "
            update '$BKG_INDEX_TBL_PKG'
            set date = '$today'
            where owner_id = '556677' and package = 'stale';
        "
    }

    owner_repo_names_from_db() {
        :
    }

    update_owner '556677/KnownOwner' >/dev/null || fail "Expected partial owner refresh to complete"
    popd >/dev/null

    [ "$(cat "$captured")" = 'container/StaleRepo/stale' ] || fail "Expected partial refresh to use the stored repository identity"
}

test_unresolved_partial_owner_refresh_reconciles_complete_listing() {
    local today
    local yesterday

    setup_discovery_fixture
    init_bkg_state
    BKG_INDEX_DB="$workdir/partial-owner-reconcile.db"
    today=$(date -u +%Y-%m-%d)
    yesterday=$(date -u -d yesterday +%Y-%m-%d)
    BKG_BATCH_FIRST_STARTED=$today
    set_BKG BKG_BATCH_FIRST_STARTED "$today"
    set_BKG BKG_BATCH_MARKER test-batch
    sqlite_ensure_index_schema >/dev/null
    command sqlite3 "$BKG_INDEX_DB" "
        insert into '$BKG_INDEX_TBL_PKG' (
            owner_id, owner_type, package_type, owner, repo, package,
            downloads, downloads_month, downloads_week, downloads_day,
            size, date
        ) values
            ('556677', 'orgs', 'container', 'KnownOwner', 'FreshRepo', 'fresh', 1, 1, 1, 1, 1, '$today'),
            ('556677', 'orgs', 'container', 'KnownOwner', 'DeletedRepo', 'deleted', 1, 1, 1, 1, 1, '$yesterday');
    "

    pushd "$workdir" >/dev/null
    printf '%s\n' "556677|KnownOwner|FreshRepo|fresh|$today" >packages_already_updated

    curl() {
        printf '%s\n' '<a href="/orgs/KnownOwner/people">people</a>'
    }

    run_parallel() {
        local function_name=$1
        local items=$2
        local item

        while IFS= read -r item; do
            [ -n "$item" ] || continue
            "$function_name" "$item"
        done <<<"$items"
    }

    update_package() {
        :
    }

    page_package() {
        return 2
    }

    query_api_optional() {
        printf '%s\n' null
    }

    owner_repo_names_from_db() {
        :
    }

    update_owner '556677/KnownOwner' >/dev/null || fail "Expected unresolved partial refresh to reconcile"
    popd >/dev/null

    [ "$(command sqlite3 "$BKG_INDEX_DB" "select count(*) from '$BKG_INDEX_TBL_PKG' where package = 'deleted';")" -eq 0 ] ||
        fail "Expected a confirmed deleted package to be reconciled after direct refresh failed"
}

test_stale_owner_scan_marker_restarts_from_first_page() {
    local marker_key
    local observed_marker_file="$workdir/observed-marker"
    local observed_page_file="$workdir/observed-page"
    local output
    local today

    setup_discovery_fixture
    init_bkg_state
    BKG_INDEX_DB="$workdir/stale-owner-scan.db"
    today=$(date -u +%Y-%m-%d)
    BKG_BATCH_FIRST_STARTED=$today
    set_BKG BKG_BATCH_FIRST_STARTED "$today"
    set_BKG BKG_BATCH_MARKER test-batch
    sqlite_ensure_index_schema >/dev/null

    pushd "$workdir" >/dev/null
    : >packages_already_updated
    owner_id=556677
    marker_key=$(owner_scan_marker_key)
    set_BKG BKG_PAGE_"$owner_id" 7
    set_BKG "$marker_key" stale-marker

    curl() {
        printf '%s\n' '<a href="/orgs/KnownOwner/people">people</a>'
    }

    page_package() {
        [ "$1" = "1" ] || fail "Expected stale owner scan to restart at page 1"
        [ "$OWNER_SCAN_MARKER" != "stale-marker" ] || fail "Expected stale owner scan marker to be replaced"
        case "$OWNER_SCAN_MARKER" in
        test-batch:556677:*) ;;
        *) fail "Expected replacement owner scan marker to include the current batch" ;;
        esac
        printf '%s\n' "$OWNER_SCAN_MARKER" >"$observed_marker_file"
        printf '%s\n' "$1" >"$observed_page_file"
        return 2
    }

    run_parallel() {
        local function_name=$1
        local items=$2
        local item

        while IFS= read -r item; do
            [ -n "$item" ] || continue
            "$function_name" "$item"
        done <<<"$items"
    }

    update_package() {
        :
    }

    query_api_optional() {
        printf '%s\n' null
    }

    owner_repo_names_from_db() {
        :
    }

    output=$(update_owner '556677/KnownOwner') || fail "Expected stale owner scan restart to complete"
    popd >/dev/null

    grep -Fq "Discarding stale owner scan marker for KnownOwner" <<<"$output" ||
        fail "Expected stale owner scan marker warning"
    [ "$(cat "$observed_page_file")" = "1" ] || fail "Expected restarted scan to visit page 1"
    [ -s "$observed_marker_file" ] || fail "Expected restarted scan to use a replacement marker"
    [ -z "$(get_BKG BKG_PAGE_556677)" ] || fail "Expected completed scan to clear resumed page state"
    [ -z "$(get_BKG "$marker_key")" ] || fail "Expected completed scan to clear owner scan marker"
}

test_page_owner_merges_deduplicated_api_pages() {
    local output_file="$workdir/page-owner-output.txt"

    setup_discovery_fixture
    init_bkg_state
    GITHUB_TOKEN=dummy
    BKG_PAGE_ALL=1
    set_BKG BKG_LAST_SCANNED_ID 0
    : >"$output_file"

    bkg_python() {
        [ "$1" = "discovery" ] || fail "Expected discovery command group"
        [ "$2" = "admit-owner-page" ] || fail "Expected admit-owner-page command"
        [ "$4" = "1" ] || fail "Expected owner page discovery to use the one-owner crawl page size"
        [ "$5" = "packages_all" ] || fail "Expected owner page admission to read packages_all"
        case "$3" in
        1)
            printf '%s\t%s\n' has_more true
            printf '%s\t%s\n' owners_count 1
            printf '%s\t%s\n' admitted_count 1
            printf '%s\t%s\n' requested alpha
            ;;
        2)
            printf '%s\t%s\n' has_more true
            printf '%s\t%s\n' owners_count 1
            printf '%s\t%s\n' admitted_count 1
            printf '%s\t%s\n' requested beta
            ;;
        3)
            printf '%s\t%s\n' has_more false
            printf '%s\t%s\n' owners_count 0
            printf '%s\t%s\n' admitted_count 0
            ;;
        *)
            fail "Unexpected owner page request: $3"
            ;;
        esac
    }

    query_api() {
        fail "Expected page_owner REST discovery to be handled by Python"
    }

    request_owner() {
        fail "Expected page_owner admission to be handled by Python"
    }

    jq() {
        fail "Expected page_owner to avoid shell JSON parsing"
    }

    page_owner 1 >>"$output_file" || fail "Expected page_owner page 1 to continue when Python reports more owners"
    page_owner 2 >>"$output_file" || fail "Expected page_owner page 2 to continue when Python reports more owners"

    if page_owner 3 >/dev/null; then
        fail "Expected page_owner to stop paging after both raw API pages are short"
    elif [ "$?" -ne 2 ]; then
        fail "Expected page_owner to return 2 when paging should stop"
    fi

    grep -Fxq "Requested alpha" "$output_file" ||
        fail "Expected page_owner to echo Python admission logs"
    grep -Fxq "Requested beta" "$output_file" ||
        fail "Expected page_owner to echo Python admission logs"
}

test_graphql_discovery_paths_avoid_html_scraping() {
    local repo_nodes
    local user_nodes
    local membership_nodes

    setup_discovery_fixture
    init_bkg_state
    GITHUB_TOKEN=dummy

    bkg_python() {
        [ "$1" = "discovery" ] || fail "Expected discovery command group"
        case "$2" in
        orgs)
            [ "$3" = ipitio ] || fail "Unexpected organization discovery owner: $3"
            [ -z "${4:-}" ] || fail "Unexpected organization discovery option: $4"
            printf '%s\n' gamma
            ;;
        explore)
            case "$3/${4:-}" in
            ipitio/backage/stargazers)
                printf '%s\n' alpha
                ;;
            ipitio/followers)
                printf '%s\n' beta gamma
                ;;
            *)
                fail "Unexpected explore request: $3/${4:-}"
                ;;
            esac
            ;;
        membership)
            [ "$3" = '1/github' ] || fail "Unexpected membership owner: $3"
            printf '%s\n' delta
            ;;
        *)
            fail "Unexpected discovery command: $2"
            ;;
        esac
    }

    query_graphql_api() {
        fail "Expected migrated discovery page helpers to avoid shell GraphQL parsing"
    }

    curl() {
        fail "Expected GraphQL discovery path to avoid HTML scraping"
    }

    repo_nodes=$(explore 'ipitio/backage' 'stargazers')
    grep -Fxq alpha <<<"$repo_nodes" || fail "Expected GraphQL repo discovery to emit stargazer logins"
    ! grep -Fxq ipitio <<<"$repo_nodes" || fail "Expected explore to continue filtering the current owner from repo discovery results"

    user_nodes=$(explore ipitio followers)
    grep -Fxq beta <<<"$user_nodes" || fail "Expected GraphQL user discovery to emit follower logins"
    grep -Fxq gamma <<<"$user_nodes" || fail "Expected GraphQL user discovery to include organization expansion output"

    user_nodes=$(curl_orgs ipitio)
    grep -Fxq gamma <<<"$user_nodes" || fail "Expected GraphQL organization discovery to emit organization logins"

    membership_nodes=$(get_membership '1/github')
    grep -Fxq delta <<<"$membership_nodes" || fail "Expected GraphQL membership discovery to emit organization member logins"
}

test_curl_orgs_ignores_blank_target() {
    local orgs

    setup_discovery_fixture
    init_bkg_state
    GITHUB_TOKEN=dummy

    bkg_python() {
        fail "Blank organization discovery should not call Python"
    }

    curl() {
        fail "Blank organization discovery should not scrape GitHub"
    }

    orgs=$(curl_orgs "")
    [ -z "$orgs" ] || fail "Expected blank organization discovery to emit no owners"
}

test_remembered_no_package_connection_owner_is_filtered_but_manual_owner_is_not() {
    local filtered_after_remember
    local filtered_manual_after_remember

    setup_discovery_fixture
    init_bkg_state
    BKG_INDEX_DB="$workdir/no-package-owners.db"
    BKG_BATCH_FIRST_STARTED="$(date -u +%Y-%m-%d)"
    mkdir -p "$BKG_INDEX_DIR"
    command sqlite3 "$BKG_INDEX_DB" "create table if not exists '$BKG_INDEX_TBL_PKG' (owner_id text, owner_type text not null, package_type text not null, owner text not null, repo text not null, package text not null, downloads integer not null, downloads_month integer not null, downloads_week integer not null, downloads_day integer not null, size integer not null, date text not null, primary key (owner_id, package, date));"
    set_BKG BKG_DISCOVERED_CONNECTION_OWNERS '4242/NoPackages'

    pushd "$workdir" >/dev/null
    : >packages_already_updated

    curl() {
        printf '%s\n' '<div></div>'
    }

    update_owner '4242/NoPackages' >/dev/null || fail "Expected update_owner to handle owners with no packages"

    [ "$(command sqlite3 "$BKG_INDEX_DB" "select count(*) from '$BKG_INDEX_TBL_OWN' where owner_id = '4242' and owner = 'NoPackages' and date = '$BKG_BATCH_FIRST_STARTED';")" = '1' ] || fail "Expected no-package owner to be remembered in the owners table for the current batch"

    printf '%s\n' NoPackages >"$connections"
    : >"$owners_file"
    command sqlite3 "$BKG_INDEX_DB" "
        with known_owners as (
            select owner from '$BKG_INDEX_TBL_PKG' where owner is not null and owner != ''
            union
            select owner from '$BKG_INDEX_TBL_OWN' where date >= '$BKG_BATCH_FIRST_STARTED' and owner is not null and owner != ''
        )
        select owner from known_owners order by owner asc;
    " >all_owners_in_db
    filtered_after_remember=$(bash "$src_dir/lib/get.sh" 0 "$connections" 10 ipitio "$owners_file" "$index_repo")
    ! grep -Fxq NoPackages <<<"$filtered_after_remember" || fail "Expected remembered no-package owner to be filtered from later batch discovery"

    printf '%s\n' NoPackages >"$owners_file"
    filtered_manual_after_remember=$(bash "$src_dir/lib/get.sh" 0 "$connections" 10 ipitio "$owners_file" "$index_repo")
    grep -Fxq NoPackages <<<"$filtered_manual_after_remember" || fail "Expected owners.txt entries to bypass remembered no-package connection filtering"

    popd >/dev/null
}

trap cleanup EXIT

source_project_script 'lib/owner.sh'

run_test test_discovered_second_hop_org_survives_owner_admission
run_test test_discovered_owner_admission_includes_all_candidates_below_cap
run_test test_save_owner_queues_resolved_owner_id
run_test test_page_package_enqueues_package
run_test test_page_package_accepts_repositoryless_package
run_test test_page_package_distinguishes_transport_failure_from_empty_listing
run_test test_partial_owner_refresh_uses_known_package_identity
run_test test_unresolved_partial_owner_refresh_reconciles_complete_listing
run_test test_stale_owner_scan_marker_restarts_from_first_page
run_test test_page_owner_merges_deduplicated_api_pages
run_test test_graphql_discovery_paths_avoid_html_scraping
run_test test_curl_orgs_ignores_blank_target
run_test test_remembered_no_package_connection_owner_is_filtered_but_manual_owner_is_not

echo "Second-hop discovery regression test passed"
