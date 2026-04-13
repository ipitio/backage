#!/bin/bash

# shellcheck disable=SC1091,SC2034

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

test_page_owner_merges_deduplicated_api_pages() {
    setup_discovery_fixture
    init_bkg_state
    GITHUB_TOKEN=dummy
    BKG_PAGE_ALL=1
    set_BKG BKG_LAST_SCANNED_ID 0

    query_api() {
        case "$1" in
        users*'&page=1&'*)
            printf '%s\n' '[{"id":1,"login":"alpha"}]'
            ;;
        organizations*'&page=1&'*)
            printf '%s\n' '[{"id":1,"login":"alpha"}]'
            ;;
        users*'&page=2&'*)
            printf '%s\n' '[{"id":2,"login":"beta"}]'
            ;;
        organizations*'&page=2&'*)
            printf '%s\n' '[]'
            ;;
        users*'&page=3&'*)
            printf '%s\n' '[]'
            ;;
        organizations*'&page=3&'*)
            printf '%s\n' '[]'
            ;;
        esac
    }

    request_owner() {
        :
    }

    jq() {
        if [[ " $* " == *" --argjson users "* ]] || [[ " $* " == *" --argjson orgs "* ]]; then
            fail "Expected page_owner to merge API pages without jq --argjson"
        fi

        command jq "$@"
    }

    page_owner 1 >/dev/null || fail "Expected page_owner page 1 to continue when both raw API pages are full but the merged owner list deduplicates to one owner"
    page_owner 2 >/dev/null || fail "Expected page_owner page 2 to continue when one raw API page is still full"

    if page_owner 3 >/dev/null; then
        fail "Expected page_owner to stop paging after both raw API pages are short"
    elif [ "$?" -ne 2 ]; then
        fail "Expected page_owner to return 2 when paging should stop"
    fi
}

test_graphql_discovery_paths_avoid_html_scraping() {
    local repo_nodes
    local user_nodes
    local membership_nodes

    setup_discovery_fixture
    init_bkg_state
    GITHUB_TOKEN=dummy

    query_graphql_api() {
        case "$1" in
        *'repository(owner:"ipitio", name:"backage")'*stargazers*)
            cat <<'EOF'
{"data":{"repository":{"stargazers":{"nodes":[{"login":"alpha"},{"login":"ipitio"}],"pageInfo":{"hasNextPage":false,"endCursor":null}}}}}
EOF
            ;;
        *'repositoryOwner(login:"ipitio")'*'__typename'*)
            cat <<'EOF'
{"data":{"owner":{"__typename":"User"}}}
EOF
            ;;
        *'repositoryOwner(login:"ipitio")'*followers*)
            cat <<'EOF'
{"data":{"owner":{"followers":{"nodes":[{"login":"beta"}],"pageInfo":{"hasNextPage":false,"endCursor":null}}}}}
EOF
            ;;
        *'repositoryOwner(login:"ipitio")'*organizations*)
            cat <<'EOF'
{"data":{"owner":{"organizations":{"nodes":[{"login":"gamma"}],"pageInfo":{"hasNextPage":false,"endCursor":null}}}}}
EOF
            ;;
        *'repositoryOwner(login:"github")'*'__typename'*)
            cat <<'EOF'
{"data":{"owner":{"__typename":"Organization"}}}
EOF
            ;;
        *'repositoryOwner(login:"github")'*membersWithRole*)
            cat <<'EOF'
{"data":{"owner":{"membersWithRole":{"nodes":[{"login":"delta"}],"pageInfo":{"hasNextPage":false,"endCursor":null}}}}}
EOF
            ;;
        *)
            fail "Unexpected GraphQL discovery query: $1"
            ;;
        esac
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

    membership_nodes=$(get_membership '1/github')
    grep -Fxq delta <<<"$membership_nodes" || fail "Expected GraphQL membership discovery to emit organization member logins"
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
run_test test_page_owner_merges_deduplicated_api_pages
run_test test_graphql_discovery_paths_avoid_html_scraping
run_test test_remembered_no_package_connection_owner_is_filtered_but_manual_owner_is_not

echo "Second-hop discovery regression test passed"