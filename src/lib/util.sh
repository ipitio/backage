#!/bin/bash
# Backage library
# Usage: ./lib.sh
# Dependencies: git curl jq parallel sqlite3 sqlite3-pcre zstd libxml2-utils, yq
# Copyright (c) ipitio
#
# shellcheck disable=SC1090,SC1091,SC2015,SC2034

set -o allexport

sudonot() {
    # shellcheck disable=SC2068
    if command -v sudo >/dev/null; then
        sudo -E "${@:-:}" || "${@:-:}"
    else
        "${@:-:}"
    fi
}

apt_install() {
    if ! dpkg -s "$@" &>/dev/null; then
        sudonot apt-get update
        sudonot apt-get install -yqq "$@"
    fi
}

yq_install() {
    [ ! -f /usr/bin/yq ] || sudonot mv -f /usr/bin/yq /usr/bin/yq.bak
    sudonot curl -LNZo /usr/bin/yq https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64
    sudonot chmod +x /usr/bin/yq
}

if [ -z "${BKG_UTIL_BOOTSTRAPPED:-}" ]; then
    if [ "${BKG_SKIP_DEP_VERIFY:-0}" != "1" ]; then
        echo "Verifying dependencies..."
        apt_install git curl jq parallel sqlite3 sqlite3-pcre zstd libxml2-utils
        yq -V | grep -q mikefarah 2>/dev/null || yq_install
        echo "Dependencies verified!"
    fi

    BKG_UTIL_BOOTSTRAPPED=1
fi
GITHUB_OWNER=${GITHUB_OWNER:-ipitio}
GITHUB_REPO=${GITHUB_REPO:-backage}
BKG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"/../..
BKG_ENV=env.env
BKG_OWNERS=$BKG_ROOT/owners.txt
BKG_OPTOUT=$BKG_ROOT/optout.txt
BKG_INDEX_TBL_OWN=owners
BKG_INDEX_TBL_PKG=packages
BKG_INDEX_TBL_VER=versions
BKG_MODE=0
BKG_MAX_LEN=14400
BKG_IS_FIRST=false
BKG_PAGE_ALL=1

# format numbers like 1000 to 1k
numfmt() {
    awk '{ split("k M B T P E Z Y", v); s=0; while( $1>999.9 ) { $1/=1000; s++ } print int($1*10)/10 v[s] }'
}

# format bytes to KB, MB, GB, etc.
numfmt_size() {
    # use sed to remove trailing \s*$
    awk '{ split("kB MB GB TB PB EB ZB YB", v); s=0; while( $1>999.9 ) { $1/=1000; s++ } print int($1*10)/10 " " v[s] }' | sed 's/[[:blank:]]*$//'
}

fmtsize_num() {
    awk '{
        if ($2) {
            u = substr($2, 1, 1)
            p = index("kMGTPEZY", u)
            if (p > 0) {
                m = match($2, /i?B$/) ? (match($2, /iB$/) ? 1024 : 1000) : (match($2, /b$/) ? 125 : 1024)
                $1 *= m ^ p
            }
        }
        print int($1)
    }'
}

sqlite3() {
    command sqlite3 -init <(echo "
.output /dev/null
.timeout 100000
.load /usr/lib/sqlite3/pcre.so
PRAGMA synchronous = OFF;
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = OFF;
PRAGMA locking_mode = NORMAL;
PRAGMA cache_size = -500000;
.output stdout
") "$@" 2>/dev/null
}

get_BKG() {
    [ -f "$BKG_ENV" ] || return
    while [ -f "$BKG_ENV.lock" ]; do sleep 0.05; done
    grep "^$1=" "$BKG_ENV" | cut -d'=' -f2
}

set_BKG() {
    local value
    local tmp_file
    value=$(echo "$2" | perl -pe 'chomp if eof')
    tmp_file=$(mktemp)
    [ -f "$BKG_ENV" ] || return
    until ln "$BKG_ENV" "$BKG_ENV.lock" 2>/dev/null; do sleep 0.05; done

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
    local list_escaped
    local code=0
    until ln "$BKG_ENV" "$BKG_ENV.$1.lock" 2>/dev/null; do sleep 0.05; done
    list=$(get_BKG_set "$1" | awk '!seen[$0]++')

    if awk -v item="$2" '$0 == item { found = 1; exit } END { exit !found }' <<<"$list"; then
        code=1
    else
        list="${list:+$list$'\n'}$2"
    fi

    list_escaped=$(perl -pe 's/\n/\\n/g; s/^\\n//' <<<"$list")
    set_BKG "$1" "$list_escaped"
    rm -f "$BKG_ENV.$1.lock"
    return $code
}

del_BKG() {
    [ -f "$BKG_ENV" ] || return
    until ln "$BKG_ENV" "$BKG_ENV.lock" 2>/dev/null; do sleep 0.05; done
    sed -i "/^$1=/d;/^\s*$/d" "$BKG_ENV"
    echo >>"$BKG_ENV"
    rm -f "$BKG_ENV.lock"
}

save_and_exit() {
    ((BKG_MAX_LEN > 0)) || return
    local to
    to=$(get_BKG BKG_TIMEOUT)

    if ((${to:-0} == 0)); then
        set_BKG BKG_TIMEOUT "1"
        echo "Stopping $$..."
    fi

    return 3
}

stop_requested() {
    [ "$(get_BKG BKG_TIMEOUT)" = "1" ]
}

sleep_with_stop_check() {
    local remaining_time=${1:-0}

    while ((remaining_time > 0)); do
        stop_requested && return 3
        sleep 1
        ((remaining_time--))
    done
}

run_command_with_stop_check() {
    local combine_output=false
    local stdout_file
    local stderr_file
    local pid
    local status
    local _

    if [ "${1:-}" = "--combine-output" ]; then
        combine_output=true
        shift
    fi

    stdout_file=$(mktemp)
    stderr_file=$(mktemp)

    "$@" >"$stdout_file" 2>"$stderr_file" &
    pid=$!

    while kill -0 "$pid" 2>/dev/null; do
        if stop_requested; then
            kill "$pid" 2>/dev/null || :

            for _ in 1 2 3 4 5; do
                kill -0 "$pid" 2>/dev/null || break
                sleep 1
            done

            kill -9 "$pid" 2>/dev/null || :
            wait "$pid" 2>/dev/null || :
            rm -f "$stdout_file" "$stderr_file"
            return 3
        fi

        sleep 1
    done

    wait "$pid"
    status=$?
    cat "$stdout_file"

    if $combine_output; then
        cat "$stderr_file"
    else
        cat "$stderr_file" >&2
    fi

    rm -f "$stdout_file" "$stderr_file"
    return "$status"
}

curl_single_attempt() {
    exec env curl -sSLNZ --connect-timeout 60 -m 120 "$@" 2>/dev/null
}

docker_manifest_inspect_once() {
    exec env docker manifest inspect -v "$1"
}

docker_manifest_inspect() {
    local manifest
    local status

    manifest=$(run_command_with_stop_check --combine-output docker_manifest_inspect_once "$1")
    status=$?
    ((status != 3)) || return 3
    echo "$manifest"
    return "$status"
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
    local rate_limit_start
    rate_limit_end=$(date -u +%s)
    rate_limit_start=$(get_BKG BKG_SCRIPT_START)
    [ -n "$rate_limit_start" ] || rate_limit_start="$BKG_SCRIPT_START"
    [ -n "$rate_limit_start" ] || echo "BKG_SCRIPT_START empty!"
    script_limit_diff=$((rate_limit_end - rate_limit_start))
    ((script_limit_diff < BKG_MAX_LEN)) || save_and_exit
    (($? != 3)) || return 3
    total_calls=$(get_BKG BKG_CALLS_TO_API)
    rate_limit_start=$(get_BKG BKG_RATE_LIMIT_START)
    [ -n "$rate_limit_start" ] || set_BKG BKG_RATE_LIMIT_START "$rate_limit_end"
    rate_limit_diff=$((rate_limit_end - rate_limit_start))
    hours_passed=$((rate_limit_diff / 3600))

    if ((total_calls >= 1000 * (hours_passed + 1))); then
        echo "$total_calls calls to the GitHub API in $((rate_limit_diff / 60)) minutes"
        remaining_time=$((3600 * (hours_passed + 1) - rate_limit_diff))
        ((remaining_time < BKG_MAX_LEN - script_limit_diff)) || save_and_exit
        (($? != 3)) || return 3
        start=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
        end=$(date -u -d "+${remaining_time} seconds" +'%Y-%m-%dT%H:%M:%SZ')
        echo "Sleeping for $remaining_time seconds from $start to $end..."
        sleep_with_stop_check "$remaining_time"
        (($? != 3)) || return 3
        echo "Resuming!"
        set_BKG BKG_RATE_LIMIT_START "$(date -u +%s)"
        set_BKG BKG_CALLS_TO_API "0"
    fi

    # wait if 900 or more calls have been made in the last minute
    minute_calls=$(get_BKG BKG_MIN_CALLS_TO_API)
    rate_limit_start=$(get_BKG BKG_MIN_RATE_LIMIT_START)
    [ -n "$rate_limit_start" ] || echo "BKG_MIN_RATE_LIMIT_START empty!"
    sec_limit_diff=$(($(date -u +%s) - rate_limit_start))
    min_passed=$((sec_limit_diff / 60))

    if ((minute_calls >= 900 * (min_passed + 1))); then
        echo "$minute_calls calls to the GitHub API in $sec_limit_diff seconds"
        remaining_time=$((60 * (min_passed + 1) - sec_limit_diff))
        ((remaining_time < BKG_MAX_LEN - script_limit_diff)) || save_and_exit
        (($? != 3)) || return 3
        start=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
        end=$(date -u -d "+${remaining_time} seconds" +'%Y-%m-%dT%H:%M:%SZ')
        echo "Sleeping for $remaining_time seconds from $start to $end..."
        sleep_with_stop_check "$remaining_time"
        (($? != 3)) || return 3
        echo "Resuming!"
        set_BKG BKG_MIN_RATE_LIMIT_START "$(date -u +%s)"
        set_BKG BKG_MIN_CALLS_TO_API "0"
    fi
}

curl() {
    # if connection times out or max time is reached, wait increasing amounts of time before retrying
    local i=2
    local max_attempts=7
    local wait_time=1
    local result
    local status

    while [ "$i" -lt "$max_attempts" ]; do
        stop_requested && return 3
        result=$(run_command_with_stop_check curl_single_attempt "$@")
        status=$?
        ((status != 3)) || return 3
        [ -n "$result" ] && echo "$result" && return 0
        stop_requested && return 3
        check_limit || return $?
        sleep_with_stop_check "$wait_time"
        (($? != 3)) || return 3
        ((i++))
        ((wait_time *= i))
    done

    echo ""
    return 1
}

run_parallel() {
    local code
    local exit_code
    exit_code=$(mktemp)

    if [ "$(wc -l <<<"$2")" -gt 1 ]; then
        ( # parallel --lb --halt soon,fail=1 -j "$max_jobs"
            local active=0
            local max_jobs
            max_jobs=$(nproc --all)

            for i in $2; do
                code=$(cat "$exit_code")
                ! grep -q "3" <<<"$code" || exit
                ! grep -q "2" <<<"$code" || break

                while [ "$active" -ge "$max_jobs" ]; do
                    wait -n
                    ((active--))
                done

                ("$1" "$i" || echo "$?" >>"$exit_code") &
                ((active++))
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

parallel_shell_func() {
    [ -n "$1" ] || return
    [ -n "$2" ] || return
    local source_file=$1
    local function_name=$2
    local status
    local stderr_file
    shift 2

    stderr_file=$(mktemp)
    parallel "$@" bash "$BKG_ROOT/src/lib/parallel-worker.sh" "$source_file" "$function_name" 2>"$stderr_file"
    status=$?

    if ((status == 2)) && [ "$(get_BKG BKG_TIMEOUT)" = "1" ]; then
        grep -Ev '^parallel: This job failed:$|^bash .*/parallel-worker\.sh .*$|^parallel: Starting no more jobs\. Waiting for [0-9]+ jobs to finish\.$' "$stderr_file" >&2 || :
        rm -f "$stderr_file"
        return 3
    fi

    cat "$stderr_file" >&2
    rm -f "$stderr_file"
    return "$status"
}

parallel_async_status() {
    [ -n "$PARALLEL_ASYNC_EXIT_CODE" ] || return
    [ -f "$PARALLEL_ASYNC_EXIT_CODE" ] || return
    ! grep -Fxq "3" "$PARALLEL_ASYNC_EXIT_CODE" || return 3
}

parallel_async_submit() {
    [ -n "$1" ] || return
    [ -n "$2" ] || return

    if [ -z "$PARALLEL_ASYNC_EXIT_CODE" ]; then
        PARALLEL_ASYNC_EXIT_CODE=$(mktemp)
        PARALLEL_ASYNC_MAX_JOBS=$(nproc --all)
        PARALLEL_ASYNC_RUNNING=0
    fi

    parallel_async_status || return $?

    while [ "$PARALLEL_ASYNC_RUNNING" -ge "$PARALLEL_ASYNC_MAX_JOBS" ]; do
        wait -n || :
        ((PARALLEL_ASYNC_RUNNING--))
        parallel_async_status || return $?
    done

    ("$1" "$2" || printf '%s\n' "$?" >>"$PARALLEL_ASYNC_EXIT_CODE") &
    ((PARALLEL_ASYNC_RUNNING++))
}

parallel_async_wait() {
    local status=0

    [ -n "$PARALLEL_ASYNC_EXIT_CODE" ] || return 0

    while ((PARALLEL_ASYNC_RUNNING > 0)); do
        wait -n || :
        ((PARALLEL_ASYNC_RUNNING--))
        parallel_async_status || status=$?
    done

    parallel_async_status || status=$?
    rm -f "$PARALLEL_ASYNC_EXIT_CODE"
    unset PARALLEL_ASYNC_EXIT_CODE PARALLEL_ASYNC_MAX_JOBS PARALLEL_ASYNC_RUNNING
    return "$status"
}

_jq() {
    echo "$1" | base64 --decode | jq -r "${@:2}"
}

dldb() {
    local latest=${1:-$(curl "https://github.com/${GITHUB_OWNER}/${GITHUB_REPO}/releases/latest" | grep -oP "href=\"/${GITHUB_OWNER}/${GITHUB_REPO}/releases/tag/[^\"]+" | cut -d'/' -f6)}
    [[ "$(curl -o /dev/null --silent -Iw '%{http_code}' "https://github.com/${GITHUB_OWNER}/${GITHUB_REPO}/releases/download/$latest/index.sql.zst")" != "404" ]] || return 1
    [ -z "$2" ] || return 0
    echo "Downloading the latest database..."
    # `cd src ; source bkg.sh && dldb` to dl the latest db
    [ ! -f "$BKG_INDEX_DB" ] || mv "$BKG_INDEX_DB" "$BKG_INDEX_DB".bak
    command curl -sSLNZ "https://github.com/${GITHUB_OWNER}/${GITHUB_REPO}/releases/download/$latest/index.sql.zst" | unzstd -v -c | sqlite3 "$BKG_INDEX_DB"

    if [ -f "$BKG_INDEX_DB" ]; then
        [ ! -f "$BKG_INDEX_DB".bak ] || rm -f "$BKG_INDEX_DB".bak
    else
        [ ! -f "$BKG_INDEX_DB".bak ] || mv "$BKG_INDEX_DB".bak "$BKG_INDEX_DB"
        echo "Failed to get the latest database"
    fi

    [ -f "$BKG_ROOT/.gitignore" ] || echo "*.db*" >>$BKG_ROOT/.gitignore
    grep -q "\*.db" "$BKG_ROOT/.gitignore" || echo "*.db*" >>$BKG_ROOT/.gitignore
}

curl_gh() {
    curl -H "Accept: application/vnd.github+json" -H "Authorization: Bearer $GITHUB_TOKEN" -H "X-GitHub-Api-Version: 2022-11-28" "$@"
}

query_api() {
    local res
    local calls_to_api
    local min_calls_to_api

    res=$(curl_gh "https://api.github.com/$1")
    (($? != 3)) || return 3
    calls_to_api=$(get_BKG BKG_CALLS_TO_API)
    min_calls_to_api=$(get_BKG BKG_MIN_CALLS_TO_API)
    ((calls_to_api++))
    ((min_calls_to_api++))
    set_BKG BKG_CALLS_TO_API "$calls_to_api"
    set_BKG BKG_MIN_CALLS_TO_API "$min_calls_to_api"
    echo "$res"
}

check_db() {
    local release
    local latest
    release=$(query_api "repos/${GITHUB_OWNER}/${GITHUB_REPO}/releases/latest")
    latest=$(jq -r '.tag_name' <<<"$release")

    until dldb "$latest" "1"; do
        echo "Deleting the latest release..."
        curl_gh -X DELETE "https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/releases/$(jq -r '.id' <<<"$release")"
        release=$(query_api "repos/${GITHUB_OWNER}/${GITHUB_REPO}/releases/latest")
        latest=$(jq -r '.tag_name' <<<"$release")
    done
}

docker_manifest_size() {
    local manifest=$1

    if [[ -n "$(jq '.. | try .layers[]' 2>/dev/null <<<"$manifest")" ]]; then
        jq '.. | try .size | select(. > 0)' <<<"$manifest" | awk '{s+=$1} END {printf "%d",  s}'
    elif [[ -n "$(jq '.. | try .manifests[]' 2>/dev/null <<<"$manifest")" ]]; then
        jq '.. | try .size | select(. > 0)' <<<"$manifest" | awk '{s+=$1} END {printf "%d",  s/NR}'
    else
        echo -1
    fi
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
            owner_id=$(jq -r '.id' <<<"$owner_id")

            if [[ ! "$owner_id" =~ ^[1-9] ]]; then
                owner_id=$(query_api "orgs/$owner")
                (($? != 3)) || return 3
                owner_id=$(jq -r '.id' <<<"$owner_id") || return 1
            fi
        fi
    fi

    echo "$owner_id/$owner"
}

owner_has_packages() {
    local owner=$1
    local owner_type
    [ -n "$(curl "https://github.com/orgs/$owner/people" | grep -zoP 'href="/orgs/'"$owner"'/people"' | tr -d '\0')" ] && owner_type="orgs" || owner_type="users"
    packages_lines=$(grep -zoP 'href="/'"$owner_type"'/'"$owner"'/packages/[^/]+/package/[^"]+"' <([ "$owner_type" = "users" ] && curl "https://github.com/$owner?tab=packages$([ "$BKG_MODE" -lt 2 ] && echo "&visibility=public" || { [ "$BKG_MODE" -eq 5 ] && echo "&visibility=private" || echo ""; })&&per_page=1&page=1" || curl "https://github.com/$owner_type/$owner/packages?per_page=1$([ "$BKG_MODE" -lt 2 ] && echo "&visibility=public" || { [ "$BKG_MODE" -eq 5 ] && echo "&visibility=private" || echo ""; })&page=1") | tr -d '\0')
    [ -n "$packages_lines" ] || return 1
}

get_owners() {
    sort -u <<<"$1" | while read -r owner; do owner_get_id "$owner"; done | grep -v '^\/'
}

curl_users() {
    local users
    users="$(curl "https://github.com/$1" | grep -oP 'href="/.+?".*>' | tr -d '\0' | grep -Ev '( .*|\?(return_to|tab))=' | tr -d '\0' | grep -oP '/.*?"' | cut -c2- | rev | cut -c2- | rev | grep -v "/")"
    (($? != 3)) || return 3
    [ -z "$2" ] && echo "$users" || get_owners "$users"
}

curl_orgs() {
    local orgs
    orgs="$(curl "https://github.com/$1" | grep -oP '/orgs/[^/]+' | tr -d '\0' | cut -d'/' -f3)"
    (($? != 3)) || return 3
    [ -z "$2" ] && echo "$orgs" || get_owners "$orgs"
}

explore() {
    local node=$1
	local is_repo=false
	local is_user=false
	local got_orgs=false
	local status=0
	[[ ! "$node" =~ .*\/.* ]] || is_repo=true
    [ "$is_repo" = true ] && local graph=("stargazers" "watchers" "forks" "collaborators") || local graph=("followers" "following" "people")
    [ -z "$2" ] || graph=("$2")

    for edge in "${graph[@]}"; do
        local page=1
        while true; do
            local nodes

            if [ "$is_repo" = true ]; then
                if [ "$edge" = "collaborators" ]; then
                    nodes=$(query_api "repos/$node/collaborators?per_page=100&page=$page" | jq -r '.[] | select(.id and .login) | "\(.id)/\(.login)"' 2>/dev/null)
                    status=$?
                else
                    nodes=$(curl_users "$node/$edge?page=$page")
                    status=$?
                fi
            else
				if [ "$is_user" = false ]; then
                	nodes=$(curl_users "orgs/$node/$edge?page=$page") # org
                    status=$?
                    ((status != 3)) || return 3
					[ -n "$nodes" ] || is_user=true
				fi

				if [ "$is_user" = true ]; then
					nodes=$(curl_users "$node?tab=$edge&page=$page") # user
                    status=$?
                    ((status != 3)) || return 3

					if [ "$got_orgs" = false ]; then
						curl_orgs "$node"
                        status=$?
                        ((status != 3)) || return 3
						got_orgs=true
					fi
				fi
            fi

            ((status != 3)) || return 3

            grep -v "$(cut -d'/' -f1 <<<"$node")" <<<"$nodes"
            [[ "$(wc -l <<<"$nodes")" -ge $([ "$edge" = "collaborators" ] && echo 100 || echo 15) ]] || break
            ((page++))
        done
    done
}

get_membership() {
    local owner
    local people_page
    owner=$(cut -d'/' -f2 <<<"$1")
    people_page=$(curl "https://github.com/orgs/$owner/people")
    (($? != 3)) || return 3

    if [ -n "$(grep -zoP 'href="/orgs/'"$owner"'/people"' <<<"$people_page" | tr -d '\0')" ]; then
        explore "$owner" "people"
    else
        curl_orgs "$owner"
    fi
}

ytox() {
    echo "<?xml version=\"1.0\" encoding=\"UTF-8\"?><xml>$(yq -ox -I0 "$1" | sed 's/"/\\"/g')</xml>" >"${1%.*}.xml" 2>/dev/null
    stat -c %s "${1%.*}.xml" || echo -1
}

ytoy() {
    yq -oy "$1" | sed 's/"/\\"/g' >"${1%.*}.yml"
}

clean_owners() {
    local temp_file
    temp_file=$(mktemp)
    echo >>"$1"
    awk 'NF' "$1" >"$temp_file" && cp -f "$temp_file" "$1"
    sed -i 's/"//g; s/^[[:space:]]*//;s/[[:space:]]*$//; /^$/d; /^0\/$/d; /^null\/.*/d; /^\(.*\/\)*\(solutions\|sponsors\|enterprise\|premium-support\)$/d' "$1"
    awk '!seen[$0]++' "$1" >"$temp_file" && cp -f "$temp_file" "$1"
}

set +o allexport
