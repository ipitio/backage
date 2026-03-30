#!/bin/bash

# shellcheck disable=SC1091,SC2034

set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
src_dir=$(cd "$script_dir/.." && pwd)
workdir=$(mktemp -d)

cleanup() {
    rm -rf "$workdir"
}

fail() {
    echo "$1" >&2
    exit 1
}

init_bkg_state() {
    local now
    now=$(date -u +%s)
    : >"$BKG_ENV"
    : >"$BKG_OWNERS"
    : >"$BKG_OPTOUT"
    mkdir -p "$BKG_INDEX_DIR"
    BKG_SCRIPT_START="$now"
    set_BKG BKG_SCRIPT_START "$now"
    set_BKG BKG_RATE_LIMIT_START "$now"
    set_BKG BKG_MIN_RATE_LIMIT_START "$now"
    set_BKG BKG_CALLS_TO_API "0"
    set_BKG BKG_MIN_CALLS_TO_API "0"
}

trap cleanup EXIT

pushd "$src_dir" >/dev/null
export BKG_SKIP_DEP_VERIFY=1
source lib/owner.sh
popd >/dev/null

BKG_ENV="$workdir/env.env"
BKG_OWNERS="$workdir/owners.txt"
BKG_OPTOUT="$workdir/optout.txt"
BKG_INDEX_DIR="$workdir/index"

connections="$workdir/connections.txt"
owners_file="$workdir/manual-owners.txt"
index_repo="$workdir/index-repo"

mkdir -p "$index_repo"
git -C "$index_repo" init -q
git -C "$index_repo" config user.name test
git -C "$index_repo" config user.email test@example.com
echo README >"$index_repo/README.md"
git -C "$index_repo" add README.md
git -C "$index_repo" commit -qm init

printf '%s\n' gianlazz Lazztech >"$connections"
: >"$owners_file"
: >"$workdir/all_owners_in_db"
: >"$workdir/owners_partially_updated"
: >"$workdir/owners_stale"

pushd "$workdir" >/dev/null
admitted=$(bash "$src_dir/lib/get.sh" 0 "$connections" 20 ipitio "$owners_file" "$index_repo")
popd >/dev/null

grep -Fxq Lazztech <<<"$admitted" || fail "Expected discovered second-hop org to survive owner admission"

init_bkg_state
save_owner "556677/Lazztech" >/dev/null
grep -Fxq "556677/Lazztech" <<<"$(get_BKG_set BKG_OWNERS_QUEUE)" || fail "Expected discovered org to be queued for owner updates"

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

grep -Fxq "container/Libre-Closet/libre-closet" <<<"$(get_BKG_set BKG_PACKAGES_Lazztech)" || fail "Expected queued package list to include Lazztech/libre-closet"

echo "Second-hop discovery regression test passed"