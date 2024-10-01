#!/bin/bash
# Backage library
# Usage: ./lib.sh
# Dependencies: curl, jq, sqlite3, zstd, parallel
# Copyright (c) ipitio
#
# shellcheck disable=SC1090,SC1091,SC2015,SC2034

if ! command -v curl &>/dev/null || ! command -v jq &>/dev/null || ! command -v sqlite3 &>/dev/null || ! command -v zstd &>/dev/null || ! command -v parallel &>/dev/null || ! command -v xmllint &>/dev/null || [ ! -f /usr/lib/sqlite3/pcre.so ]; then
    echo "Installing dependencies..."
    sudo apt-get update
    sudo apt-get install curl jq parallel sqlite3 sqlite3-pcre zstd libxml2-utils -y
fi

if ! yq -V | grep -q mikefarah; then
    echo "Installing yq..."
    sudo rm -f /usr/bin/yq
    wget https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64 -O yq
    sudo mv yq /usr/bin/yq
    sudo chmod +x /usr/bin/yq
fi

echo "Dependencies verified!"
# shellcheck disable=SC2046
source $(which env_parallel.bash)
env_parallel --session
BKG_ROOT=..
BKG_ENV=env.env
BKG_OWNERS=$BKG_ROOT/owners.txt
BKG_OPTOUT=$BKG_ROOT/optout.txt
BKG_INDEX_DB=$BKG_ROOT/index.db
BKG_INDEX_SQL=$BKG_ROOT/index.sql
BKG_INDEX_DIR=$BKG_ROOT/index
BKG_INDEX_TBL_OWN=owners
BKG_INDEX_TBL_PKG=packages
BKG_INDEX_TBL_VER=versions

# format numbers like 1000 to 1k
numfmt() {
    awk '{ split("k M B T P E Z Y", v); s=0; while( $1>999.9 ) { $1/=1000; s++ } print int($1*10)/10 v[s] }'
}

# format bytes to KB, MB, GB, etc.
numfmt_size() {
    # use sed to remove trailing \s*$
    awk '{ split("kB MB GB TB PB EB ZB YB", v); s=0; while( $1>999.9 ) { $1/=1000; s++ } print int($1*10)/10 " " v[s] }' | sed 's/[[:blank:]]*$//'
}

sqlite3() {
    command sqlite3 -init <(echo "
.output /dev/null
.timeout 100000
.load /usr/lib/sqlite3/pcre.so
PRAGMA synchronous = OFF;
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = MEMORY;
PRAGMA locking_mode = EXCLUSIVE;
PRAGMA cache_size = -500000;
.output stdout
") "$@" 2>/dev/null
}

get_BKG() {
    [ -f "$BKG_ENV" ] || return
    while [ -f "$BKG_ENV.lock" ]; do :; done
    grep "^$1=" "$BKG_ENV" | cut -d'=' -f2
}

set_BKG() {
    local value
    local tmp_file
    value=$(echo "$2" | perl -pe 'chomp if eof')
    tmp_file=$(mktemp)
    [ -f "$BKG_ENV" ] || return
    while ! ln "$BKG_ENV" "$BKG_ENV.lock" 2>/dev/null; do :; done

    if ! grep -q "^$1=" "$BKG_ENV"; then
        echo "$1=$value" >>"$BKG_ENV"
    else
        grep -v "^$1=" "$BKG_ENV" >"$tmp_file"
        echo "$1=$value" >>"$tmp_file"
        mv "$tmp_file" "$BKG_ENV"
    fi

    sed -i '/^\s*$/d' "$BKG_ENV"
    echo >>"$BKG_ENV"
    rm -f "$BKG_ENV.lock"
}

get_BKG_set() {
    get_BKG "$1" | perl -pe 's/^\\n//' | perl -pe 's/\\n$//' | perl -pe 's/\\n\\n/\\n/' | perl -pe 's/\\n/\n/g'
}

set_BKG_set() {
    local list
    local code=0
    while ! ln "$BKG_ENV" "$BKG_ENV.$1.lock" 2>/dev/null; do :; done
    list=$(get_BKG_set "$1" | awk '!seen[$0]++' | perl -pe 's/\n/\\n/g')
    # shellcheck disable=SC2076
    [[ "$list" =~ "$2" ]] && code=1 || list="${list:+$list\n}$2"
    set_BKG "$1" "$(echo "$list" | perl -pe 's/\\n/\n/g' | perl -pe 's/\n/\\n/g' | perl -pe 's/^\\n//')"
    rm -f "$BKG_ENV.$1.lock"
    return $code
}

del_BKG() {
    [ -f "$BKG_ENV" ] || return
    while ! ln "$BKG_ENV" "$BKG_ENV.lock" 2>/dev/null; do :; done
    parallel "sed -i '/^{}=/d' $BKG_ENV" ::: "$@"
    sed -i '/^\s*$/d' "$BKG_ENV"
    echo >>"$BKG_ENV"
    rm -f "$BKG_ENV.lock"
}

save_and_exit() {
    if (($(get_BKG BKG_TIMEOUT) == 0)); then
        set_BKG BKG_TIMEOUT "1"
        echo "Stopping $$..."
    fi

    return 3
}

# shellcheck disable=SC2120
check_limit() {
    local total_calls
    local rate_limit_end
    local script_limit_diff
    local rate_limit_diff
    local hours_passed
    local remaining_time
    local minute_calls
    local sec_limit_diff
    local min_passed
    local max_len=${1:-18000}
    total_calls=$(get_BKG BKG_CALLS_TO_API)
    rate_limit_end=$(date -u +%s)
    script_limit_diff=$((rate_limit_end - $(get_BKG BKG_SCRIPT_START)))
    hours_passed=$((rate_limit_diff / 3600))
    ((script_limit_diff < max_len)) || save_and_exit
    (($? != 3)) || return 3

    if ((total_calls >= 1000 * (hours_passed + 1))); then
        echo "$total_calls calls to the GitHub API in $((rate_limit_diff / 60)) minutes"
        remaining_time=$((3600 * (hours_passed + 1) - rate_limit_diff))
        ((remaining_time < max_len - script_limit_diff)) || save_and_exit
        (($? != 3)) || return 3
        echo "Sleeping for $remaining_time seconds..."
        sleep $remaining_time
        echo "Resuming!"
        set_BKG BKG_RATE_LIMIT_START "$(date -u +%s)"
        set_BKG BKG_CALLS_TO_API "0"
    fi

    # wait if 900 or more calls have been made in the last minute
    minute_calls=$(get_BKG BKG_MIN_CALLS_TO_API)
    rate_limit_end=$(date -u +%s)
    sec_limit_diff=$((rate_limit_end - $(get_BKG BKG_MIN_RATE_LIMIT_START)))
    min_passed=$((sec_limit_diff / 60))

    if ((minute_calls >= 900 * (min_passed + 1))); then
        echo "$minute_calls calls to the GitHub API in $sec_limit_diff seconds"
        remaining_time=$((60 * (min_passed + 1) - sec_limit_diff))
        ((remaining_time < max_len - script_limit_diff)) || save_and_exit
        (($? != 3)) || return 3
        echo "Sleeping for $remaining_time seconds..."
        sleep $remaining_time
        echo "Resuming!"
        set_BKG BKG_MIN_RATE_LIMIT_START "$(date -u +%s)"
        set_BKG BKG_MIN_CALLS_TO_API "0"
    fi
}

curl() {
    # if connection times out or max time is reached, wait increasing amounts of time before retrying
    local i=0
    local max_attempts=10
    local wait_time=1
    local result

    while [ "$i" -lt "$max_attempts" ]; do
        result=$(command curl -sSLNZ --connect-timeout 60 -m 120 "$@" 2>/dev/null)
        [ -n "$result" ] && echo "$result" && return 0
        check_limit || return $?
        sleep "$wait_time"
        ((i++))
        ((wait_time *= 2))
    done

    return 1
}

run_parallel() {
    local code
    local exit_code
    exit_code=$(mktemp)

    if [ "$(wc -l <<<"$2")" -gt 1 ]; then
        ( # parallel --lb --halt soon,fail=1
            for i in $2; do
                code=$(cat "$exit_code")
                ! grep -q "3" <<<"$code" || exit
                ! grep -q "2" <<<"$code" || break
                ("$1" "$i" || echo "$?" >>"$exit_code") &
            done

            wait
        ) &

        wait "$!"
    else
        "$1" "$2" || echo "$?" >>"$exit_code"
    fi

    code=$(cat "$exit_code")
    rm -f "$exit_code"
    ! grep -q "3" <<<"$code" || return 3
}

_jq() {
    echo "$1" | base64 --decode | jq -r "${@:2}"
}

dldb() {
    local code=0
    echo "Downloading the latest database..."
    # `cd src ; source bkg.sh && dldb` to dl the latest db
    [ ! -f "$BKG_INDEX_DB" ] || mv "$BKG_INDEX_DB" "$BKG_INDEX_DB".bak
    command curl -sSLNZ "https://github.com/ipitio/backage/releases/download/$(curl "https://github.com/ipitio/backage/releases/latest" | grep -oP 'href="/ipitio/backage/releases/tag/[^"]+' | cut -d'/' -f6)/index.sql.zst" | unzstd -v -c | sqlite3 "$BKG_INDEX_DB"

    if [ -f "$BKG_INDEX_DB" ]; then
        [ ! -f "$BKG_INDEX_DB".bak ] || rm -f "$BKG_INDEX_DB".bak
    else
        [ ! -f "$BKG_INDEX_DB".bak ] || mv "$BKG_INDEX_DB".bak "$BKG_INDEX_DB"
        echo "Failed to download the latest database"
        curl "https://github.com/ipitio/backage/releases/latest" | grep -q "index.sql.zst" || code=1
    fi

    [ -f "$BKG_ROOT/.gitignore" ] || echo "index.db*" >>$BKG_ROOT/.gitignore
    grep -q "index.db" "$BKG_ROOT/.gitignore" || echo "index.db*" >>$BKG_ROOT/.gitignore
    return $code
}

query_api() {
    local res
    local calls_to_api
    local min_calls_to_api
    res=$(curl -H "Accept: application/vnd.github+json" -H "Authorization: Bearer $GITHUB_TOKEN" -H "X-GitHub-Api-Version: 2022-11-28" -X "${2:-GET}" "https://api.github.com/$1")
    (($? != 3)) || return 3
    calls_to_api=$(get_BKG BKG_CALLS_TO_API)
    min_calls_to_api=$(get_BKG BKG_MIN_CALLS_TO_API)
    ((calls_to_api++))
    ((min_calls_to_api++))
    set_BKG BKG_CALLS_TO_API "$calls_to_api"
    set_BKG BKG_MIN_CALLS_TO_API "$min_calls_to_api"
    echo "$res"
}

get_db() {
    while ! dldb; do
        check_limit || return $?
        echo "Deleting the latest release..."
        query_api "repos/ipitio/backage/releases/$(query_api "repos/ipitio/backage/releases/latest" | jq -r '.id')" DELETE
        (($? != 3)) || return 3
    done
}

docker_manifest_size() {
    local manifest=$1

    if [[ -n "$(jq '.. | try .layers[]' 2>/dev/null <<<"$manifest")" ]]; then
        jq '.. | try .size | select(. > 0)' <<<"$manifest" | awk '{s+=$1} END {print s}'
    elif [[ -n "$(jq '.. | try .manifests[]' 2>/dev/null <<<"$manifest")" ]]; then
        jq '.. | try .size | select(. > 0)' <<<"$manifest" | awk '{s+=$1} END {print s/NR}'
    else
        echo -1
    fi
}

docker_manifest_tags() {
    local manifest=$1
    local version_tags
    version_tags=$(_jq "$manifest" '.. | try .tags')
    [ -n "$version_tags" ] || version_tags=$(_jq "$1" '.. | try .tags | join(",")')
    [[ ! "$version_tags" =~ null ]] || version_tags=""
    echo "$version_tags"
}

owner_get_id() {
    local owner
    local owner_id=""
    owner=$(echo "$1" | tr -d '[:space:]')
    [ -n "$owner" ] || return

    if [[ "$owner" =~ .*\/.* ]]; then
        owner_id=$(cut -d'/' -f1 <<<"$owner")
        owner=$(cut -d'/' -f2 <<<"$owner")
    fi

    if [[ ! "$owner_id" =~ ^[1-9] ]]; then
        owner_id=$(curl "https://github.com/$owner" | grep -zoP 'meta.*?u\/\d+' | tr -d '\0' | grep -oP 'u\/\d+' | sort -u | head -n1 | grep -oP '\d+')

        if [[ ! "$owner_id" =~ ^[1-9] && -n "$GITHUB_TOKEN" ]]; then
            owner_id=$(query_api "users/$owner")
            (($? != 3)) || return 3
            owner_id=$(jq -r '.id' <<<"$owner_id") || return 1
        fi
    fi

    echo "$owner_id/$owner"
}

owner_has_packages() {
    local owner=$1
    local owner_type
    [ -n "$(curl "https://github.com/orgs/$owner/people" | grep -zoP 'href="/orgs/'"$owner"'/people"' | tr -d '\0')" ] && owner_type="orgs" || owner_type="users"
    packages_lines=$(grep -zoP 'href="/'"$owner_type"'/'"$owner"'/packages/[^/]+/package/[^"]+"' <([ "$owner_type" = "users" ] && curl "https://github.com/$owner?tab=packages&visibility=public&&per_page=1&page=1" || curl "https://github.com/$owner_type/$owner/packages?visibility=public&per_page=1&page=1") | tr -d '\0')
    [ -n "$packages_lines" ] || return 1
}

get_owners() {
    sort -u <<<"$1" | while read -r owner; do owner_get_id "$owner"; done | grep -v '^\/'
}

curl_users() {
    local users
    users="$(curl "https://github.com/$1" | grep -oP 'href="/.+?".*>' | tr -d '\0' | grep -Ev '( .*|\?(return_to|tab))=' | tr -d '\0' | grep -oP '/.*?"' | cut -c2- | rev | cut -c2- | rev | grep -v "/")"
    [ -n "$1" ] && echo "$users" || get_owners "$users"
}

curl_orgs() {
    local orgs
    orgs="$(curl "https://github.com/$1" | grep -oP '/orgs/[^/]+' | tr -d '\0' | cut -d'/' -f3)"
    [ -n "$1" ] && echo "$orgs" || get_owners "$orgs"
}

explore() {
    local node=$1
    [[ "$node" =~ .*\/.* ]] && local graph=("stargazers" "watchers" "forks") || local graph=("followers" "following" "people")

    for edge in "${graph[@]}"; do
        local page=1
        while true; do
            local nodes

            if [[ "$node" =~ .*\/.* ]]; then
                nodes=$(curl_users "$node/$edge?page=$page" 1) # repo
            else
                nodes=$(curl_users "orgs/$node/$edge?page=$page" 1) # org

                if [ -z "$nodes" ]; then
                    nodes=$(curl_users "$node?tab=$edge&page=$page" 1) # user
                    curl_orgs "$1" 1
                fi
            fi

            grep -v "$(cut -d'/' -f1 <<<"$node")" <<<"$nodes"
            [[ "$(wc -l <<<"$nodes")" -ge 15 ]] || break
            ((page++))
        done
    done
}

ytox() {
    echo "<?xml version=\"1.0\" encoding=\"UTF-8\"?><bkg>$(yq -ox -I0 "$1" | sed 's/"/\\"/g')</bkg>" >"${1%.*}.xml"
}

ytoy() {
    yq -oy "$1" | sed 's/"/\\"/g' >"${1%.*}.yml"
}

echo "Checking environment..."
[ -n "$(get_BKG BKG_RATE_LIMIT_START)" ] || set_BKG BKG_RATE_LIMIT_START "0"
[ -n "$(get_BKG BKG_CALLS_TO_API)" ] || set_BKG BKG_CALLS_TO_API "0"

# reset the rate limit if an hour has passed since the last run started
if (($(get_BKG BKG_RATE_LIMIT_START) + 3600 <= $(date -u +%s))); then
    set_BKG BKG_RATE_LIMIT_START "$(date -u +%s)"
    set_BKG BKG_CALLS_TO_API "0"
fi

# reset the secondary rate limit if a minute has passed since the last run started
if (($(get_BKG BKG_MIN_RATE_LIMIT_START) + 60 <= $(date -u +%s))); then
    set_BKG BKG_MIN_RATE_LIMIT_START "$(date -u +%s)"
    set_BKG BKG_MIN_CALLS_TO_API "0"
fi
