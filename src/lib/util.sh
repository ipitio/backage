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

db_snapshot_archive_file() {
    [ -n "${BKG_INDEX_DB:-}" ] || return 1
    printf '%s\n' "${BKG_INDEX_DB}.zst"
}

legacy_sql_snapshot_archive_file() {
    if [ -n "${BKG_INDEX_SQL:-}" ]; then
        printf '%s.zst\n' "$BKG_INDEX_SQL"
    elif [ -n "${BKG_INDEX_DB:-}" ]; then
        printf '%s.sql.zst\n' "${BKG_INDEX_DB%.db}"
    else
        return 1
    fi
}

db_snapshot_asset_name() {
    basename "$(db_snapshot_archive_file)"
}

legacy_sql_snapshot_asset_name() {
    basename "$(legacy_sql_snapshot_archive_file)"
}

resolve_release_snapshot_asset() {
    local latest=$1
    local db_asset_name
    local legacy_asset_name
    local status_code

    db_asset_name=$(db_snapshot_asset_name 2>/dev/null || echo "index.db.zst")
    status_code=$(curl -o /dev/null --silent -Iw '%{http_code}' "https://github.com/${GITHUB_OWNER}/${GITHUB_REPO}/releases/download/$latest/$db_asset_name")
    if [ "$status_code" != "404" ]; then
        printf 'db|%s\n' "$db_asset_name"
        return 0
    fi

    legacy_asset_name=$(legacy_sql_snapshot_asset_name 2>/dev/null || echo "index.sql.zst")
    status_code=$(curl -o /dev/null --silent -Iw '%{http_code}' "https://github.com/${GITHUB_OWNER}/${GITHUB_REPO}/releases/download/$latest/$legacy_asset_name")
    if [ "$status_code" != "404" ]; then
        printf 'sql|%s\n' "$legacy_asset_name"
        return 0
    fi

    return 1
}

index_worktree_is_git_repo() {
    [ -n "${BKG_INDEX_DIR:-}" ] || return 1
    git -C "$BKG_INDEX_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1
}

index_sparse_set_root() {
    index_worktree_is_git_repo || return 0
    git -C "$BKG_INDEX_DIR" sparse-checkout init --cone >/dev/null 2>&1 || return 1
    git -C "$BKG_INDEX_DIR" sparse-checkout set >/dev/null 2>&1 || return 1
}

index_sparse_log_batch() {
    local batch_number=$1
    local path_count=$2
    local started_at=${3:-0}
    local elapsed=0

    ((started_at > 0)) || return 0
    elapsed=$(( $(date -u +%s) - started_at ))
    echo "Sparse expansion batch '$batch_number' ($path_count path(s)) completed in ${elapsed}s"
}

index_sparse_add_paths() {
    index_worktree_is_git_repo || return 0
    local path
    local -a batch=()
    local batch_number=0
    local batch_started_at=0

    while IFS= read -r path; do
        [ -n "$path" ] || continue
        batch+=("$path")

        if ((${#batch[@]} >= 100)); then
            ((batch_number++))
            batch_started_at=$(date -u +%s)
            git -C "$BKG_INDEX_DIR" sparse-checkout add --skip-checks -- "${batch[@]}" || return 1
            index_sparse_log_batch "$batch_number" "${#batch[@]}" "$batch_started_at"
            batch=()
        fi
    done

    if ((${#batch[@]} > 0)); then
        ((batch_number++))
        batch_started_at=$(date -u +%s)
        git -C "$BKG_INDEX_DIR" sparse-checkout add --skip-checks -- "${batch[@]}" || return 1
        index_sparse_log_batch "$batch_number" "${#batch[@]}" "$batch_started_at"
    fi
}

index_queue_owner_names() {
    get_BKG_set BKG_OWNERS_QUEUE | cut -d'/' -f2 | awk 'NF && !seen[$0]++'
}

materialize_index_queue_owners() {
    index_worktree_is_git_repo || return 0
    index_queue_owner_names | index_sparse_add_paths
}

index_top_level_owner_count() {
    index_worktree_is_git_repo || {
        echo 0
        return 0
    }

    git -C "$BKG_INDEX_DIR" ls-tree -d --name-only HEAD 2>/dev/null | awk 'NF' | wc -l
}

sqlite3() {
    local statement="${!#:-}"
    local busy_timeout_ms=${BKG_SQLITE_BUSY_TIMEOUT_MS:-300000}
    local max_attempts=${BKG_SQLITE_MAX_ATTEMPTS:-3}
    local retry_delay_secs=${BKG_SQLITE_RETRY_DELAY_SECS:-1}
    local init_file
    local stdout_file
    local stderr_file
    local attempt=1
    local status=0
    local retryable_write=false

    init_file=$(mktemp)
    stdout_file=$(mktemp)
    stderr_file=$(mktemp)

    cat >"$init_file" <<EOF
.output /dev/null
.timeout $busy_timeout_ms
.load /usr/lib/sqlite3/pcre.so
PRAGMA busy_timeout = $busy_timeout_ms;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA locking_mode = NORMAL;
PRAGMA temp_store = MEMORY;
PRAGMA wal_autocheckpoint = 1000;
PRAGMA cache_size = -500000;
.output stdout
EOF

    if (( $# >= 2 )) && [[ "$statement" =~ ^[[:space:]]*(insert|update|delete|replace|create|drop|alter|pragma|vacuum|reindex|begin|commit|rollback)([[:space:];]|$) ]]; then
        retryable_write=true
    fi

    while true; do
        command sqlite3 -init "$init_file" "$@" >"$stdout_file" 2>"$stderr_file"
        status=$?
        ((status == 0)) && break
        $retryable_write || break
        grep -Eqi 'database is locked|database is busy|database schema is locked|locking protocol|cannot commit transaction|disk i/o error' "$stderr_file" || break
        ((attempt < max_attempts)) || break
        stop_requested && {
            rm -f "$init_file" "$stdout_file" "$stderr_file"
            return 3
        }
        sleep_with_stop_check "$retry_delay_secs"
        (($? != 3)) || {
            rm -f "$init_file" "$stdout_file" "$stderr_file"
            return 3
        }
        ((attempt++))
    done

    ((status == 0)) && cat "$stdout_file"
    rm -f "$init_file" "$stdout_file" "$stderr_file"
    return "$status"
}

sqlite_escape_literal() {
    printf '%s' "$1" | sed "s/'/''/g"
}

sqlite_escape_identifier() {
    printf '%s' "$1" | sed 's/"/""/g'
}

cleanup_generated_json_sidecars() {
    [ -n "$1" ] || return
    [ -e "$1" ] || return 0

    find "$1" -type f \( -name '*.json.tmp' -o -name '*.json.abs' -o -name '*.json.rel' \) -delete
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

daily_gate_completed_today() {
    [ -n "$1" ] || return 1
    local today_value=${2:-$(date -u +%Y-%m-%d)}
    [ "$(get_BKG "$1")" = "$today_value" ]
}

mark_daily_gate_completed() {
    [ -n "$1" ] || return 1
    local today_value=${2:-$(date -u +%Y-%m-%d)}
    set_BKG "$1" "$today_value"
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

filter_running_pids() {
    local pid

    for pid in "$@"; do
        kill -0 "$pid" 2>/dev/null && printf '%s\n' "$pid"
    done
}

terminate_pids_with_grace() {
    local pid
    local _
    local -a active_pids=()

    for pid in "$@"; do
        kill -0 "$pid" 2>/dev/null || continue
        kill "$pid" 2>/dev/null || :
        active_pids+=("$pid")
    done

    for _ in 1 2 3 4 5; do
        mapfile -t active_pids < <(filter_running_pids "${active_pids[@]}")
        ((${#active_pids[@]} == 0)) && break
        sleep 1
    done

    for pid in "${active_pids[@]}"; do
        kill -9 "$pid" 2>/dev/null || :
    done

    for pid in "$@"; do
        wait "$pid" 2>/dev/null || :
    done
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
    local max_jobs
    local item
    local stop_now=false
    local -a active_pids=()
    exit_code=$(mktemp)
    max_jobs=$(command nproc --all)

    while IFS= read -r item; do
        [ -n "$item" ] || continue
        code=$(cat "$exit_code")

        if grep -q "3" <<<"$code" || stop_requested; then
            printf '%s\n' 3 >>"$exit_code"
            terminate_pids_with_grace "${active_pids[@]}"
            active_pids=()
            stop_now=true
            break
        fi

        grep -q "2" <<<"$code" && break

        while ((${#active_pids[@]} >= max_jobs)); do
            mapfile -t active_pids < <(filter_running_pids "${active_pids[@]}")
            code=$(cat "$exit_code")

            if grep -q "3" <<<"$code" || stop_requested; then
                printf '%s\n' 3 >>"$exit_code"
                terminate_pids_with_grace "${active_pids[@]}"
                active_pids=()
                stop_now=true
                break
            fi

            ((${#active_pids[@]} < max_jobs)) && break
            sleep 1
        done

        $stop_now && break

        ("$1" "$item" || echo "$?" >>"$exit_code") &
        active_pids+=("$!")
    done <<<"$2"

    while ((${#active_pids[@]} > 0)); do
        mapfile -t active_pids < <(filter_running_pids "${active_pids[@]}")
        code=$(cat "$exit_code")

        if grep -q "3" <<<"$code" || stop_requested; then
            printf '%s\n' 3 >>"$exit_code"
            terminate_pids_with_grace "${active_pids[@]}"
            active_pids=()
            break
        fi

        ((${#active_pids[@]} == 0)) && break
        sleep 1
    done

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

    if ((status == 2 || status == 3)) && [ "$(get_BKG BKG_TIMEOUT)" = "1" ]; then
        grep -Ev '^parallel: This job failed:$|^bash .*/parallel-worker\.sh .*$|^parallel: Starting no more jobs\. Waiting for [0-9]+ jobs to finish\.$' "$stderr_file" >&2 || :
        rm -f "$stderr_file"
        return 3
    fi

    cat "$stderr_file" >&2
    rm -f "$stderr_file"
    return "$status"
}

parallel_async_status() {
    PARALLEL_ASYNC_LAST_STATUS=0
    [ -n "${PARALLEL_ASYNC_EXIT_CODE:-}" ] || return
    [ -f "${PARALLEL_ASYNC_EXIT_CODE:-}" ] || return
    local async_status

    async_status=$(grep -E '^[0-9]+$' "$PARALLEL_ASYNC_EXIT_CODE" | tail -n1)
    [ -n "$async_status" ] || return 0
    PARALLEL_ASYNC_LAST_STATUS=$async_status
    ((async_status == 3)) && return 3
    return 0
}

parallel_async_default_max_jobs() {
    local max_jobs

    if [[ "${BKG_PARALLEL_ASYNC_MAX_JOBS:-}" =~ ^[1-9][0-9]*$ ]]; then
        echo "$BKG_PARALLEL_ASYNC_MAX_JOBS"
        return
    fi

    max_jobs=$(nproc --all)
    ((max_jobs *= 2))
    ((max_jobs > 0)) || max_jobs=1
    echo "$max_jobs"
}

parallel_async_submit() {
    [ -n "$1" ] || return
    [ -n "$2" ] || return
    local pid
    local async_status=0

    if [ -z "${PARALLEL_ASYNC_EXIT_CODE:-}" ]; then
        PARALLEL_ASYNC_EXIT_CODE=$(mktemp)
        PARALLEL_ASYNC_MAX_JOBS=$(parallel_async_default_max_jobs)
        PARALLEL_ASYNC_RUNNING=0
        PARALLEL_ASYNC_PIDS=()
    fi

    parallel_async_status || return $?

    while [ "$PARALLEL_ASYNC_RUNNING" -ge "$PARALLEL_ASYNC_MAX_JOBS" ]; do
        mapfile -t PARALLEL_ASYNC_PIDS < <(filter_running_pids "${PARALLEL_ASYNC_PIDS[@]}")
        PARALLEL_ASYNC_RUNNING=${#PARALLEL_ASYNC_PIDS[@]}
        parallel_async_status || {
            async_status=$?
            terminate_pids_with_grace "${PARALLEL_ASYNC_PIDS[@]}"
            PARALLEL_ASYNC_PIDS=()
            PARALLEL_ASYNC_RUNNING=0
            return "$async_status"
        }

        if ((PARALLEL_ASYNC_LAST_STATUS != 0)); then
            :
        fi

        if stop_requested; then
            printf '%s\n' 3 >>"$PARALLEL_ASYNC_EXIT_CODE"
            terminate_pids_with_grace "${PARALLEL_ASYNC_PIDS[@]}"
            PARALLEL_ASYNC_PIDS=()
            PARALLEL_ASYNC_RUNNING=0
            return 3
        fi

        [ "$PARALLEL_ASYNC_RUNNING" -lt "$PARALLEL_ASYNC_MAX_JOBS" ] || sleep 1
    done

    ("$1" "$2" || printf '%s\n' "$?" >>"$PARALLEL_ASYNC_EXIT_CODE") &
    pid=$!
    PARALLEL_ASYNC_PIDS+=("$pid")
    PARALLEL_ASYNC_RUNNING=${#PARALLEL_ASYNC_PIDS[@]}
}

parallel_async_wait() {
    local status=0
    local async_status=0

    [ -n "${PARALLEL_ASYNC_EXIT_CODE:-}" ] || return 0

    while ((PARALLEL_ASYNC_RUNNING > 0)); do
        mapfile -t PARALLEL_ASYNC_PIDS < <(filter_running_pids "${PARALLEL_ASYNC_PIDS[@]}")
        PARALLEL_ASYNC_RUNNING=${#PARALLEL_ASYNC_PIDS[@]}

        if stop_requested; then
            printf '%s\n' 3 >>"$PARALLEL_ASYNC_EXIT_CODE"
            terminate_pids_with_grace "${PARALLEL_ASYNC_PIDS[@]}"
            PARALLEL_ASYNC_PIDS=()
            PARALLEL_ASYNC_RUNNING=0
            status=3
            break
        fi

        parallel_async_status || {
            async_status=$?
            status=$async_status
            terminate_pids_with_grace "${PARALLEL_ASYNC_PIDS[@]}"
            PARALLEL_ASYNC_PIDS=()
            PARALLEL_ASYNC_RUNNING=0
            break
        }

        if ((PARALLEL_ASYNC_LAST_STATUS != 0)); then
            status=$PARALLEL_ASYNC_LAST_STATUS
        fi

        ((PARALLEL_ASYNC_RUNNING > 0)) && sleep 1
    done

    parallel_async_status || status=$?
    if ((PARALLEL_ASYNC_LAST_STATUS != 0)) && ((status == 0)); then
        status=$PARALLEL_ASYNC_LAST_STATUS
    fi
    rm -f "$PARALLEL_ASYNC_EXIT_CODE"
    unset PARALLEL_ASYNC_EXIT_CODE PARALLEL_ASYNC_MAX_JOBS PARALLEL_ASYNC_RUNNING PARALLEL_ASYNC_PIDS PARALLEL_ASYNC_LAST_STATUS
    return "$status"
}

_jq() {
    echo "$1" | base64 --decode | jq -r "${@:2}"
}

dldb() {
    local latest=${1:-$(curl "https://github.com/${GITHUB_OWNER}/${GITHUB_REPO}/releases/latest" | grep -oP "href=\"/${GITHUB_OWNER}/${GITHUB_REPO}/releases/tag/[^\"]+" | cut -d'/' -f6)}
    local asset_info
    local asset_kind
    local asset_name
    local asset_url
    local db_archive_file
    local legacy_archive_file
    local db_tmp=""
    local archive_tmp=""

    asset_info=$(resolve_release_snapshot_asset "$latest") || return 1
    asset_kind=$(cut -d'|' -f1 <<<"$asset_info")
    asset_name=$(cut -d'|' -f2 <<<"$asset_info")
    asset_url="https://github.com/${GITHUB_OWNER}/${GITHUB_REPO}/releases/download/$latest/$asset_name"
    [ -z "$2" ] || return 0
    echo "Downloading the latest database..."
    # `cd src ; source bkg.sh && dldb` to dl the latest db
    [ ! -f "$BKG_INDEX_DB" ] || mv "$BKG_INDEX_DB" "$BKG_INDEX_DB".bak

    if [ "$asset_kind" = "db" ]; then
        db_archive_file=$(db_snapshot_archive_file)
        archive_tmp=$(mktemp "$(dirname "$db_archive_file")/.${db_archive_file##*/}.XXXXXX") || return 1
        db_tmp=$(mktemp "$(dirname "$BKG_INDEX_DB")/.${BKG_INDEX_DB##*/}.XXXXXX") || {
            rm -f "$archive_tmp"
            return 1
        }

        if command curl -sSLNZ "$asset_url" -o "$archive_tmp" && unzstd -c "$archive_tmp" >"$db_tmp"; then
            mv -f "$db_tmp" "$BKG_INDEX_DB"
            mv -f "$archive_tmp" "$db_archive_file"
            legacy_archive_file=$(legacy_sql_snapshot_archive_file 2>/dev/null || :)
            [ -z "$legacy_archive_file" ] || rm -f "$legacy_archive_file"
            if command -v db_restore_signature_file >/dev/null 2>&1; then
                sha256sum "$db_archive_file" | awk '{print $1}' >"$(db_restore_signature_file)"
            fi
        else
            rm -f "$db_tmp" "$archive_tmp"
        fi
    else
        command curl -sSLNZ "$asset_url" | unzstd -v -c | command sqlite3 "$BKG_INDEX_DB"
    fi

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

graphql_query_with_rate_limit() {
    [ -n "$1" ] || return

    if grep -q 'rateLimit' <<<"$1"; then
        printf '%s\n' "$1"
        return 0
    fi

    perl -0pe 's/\}\s*$/ rateLimit { cost remaining resetAt } }/' <<<"$1"
}

query_graphql_api() {
    local query=$1
    local query_with_rate_limit
    local payload
    local res
    local calls_to_api
    local min_calls_to_api
    local graphql_cost=1
    local graphql_remaining=""
    local graphql_reset_at=""

    query_with_rate_limit=$(graphql_query_with_rate_limit "$query") || return 1
    payload=$(jq -cn --arg query "$query_with_rate_limit" '{query:$query}') || return 1
    res=$(curl_gh -X POST "https://api.github.com/graphql" -d "$payload")
    (($? != 3)) || return 3
    graphql_cost=$(jq -r '.data.rateLimit.cost // 1' <<<"$res" 2>/dev/null)
    [[ "$graphql_cost" =~ ^[0-9]+$ ]] || graphql_cost=1
    graphql_remaining=$(jq -r '.data.rateLimit.remaining // empty' <<<"$res" 2>/dev/null)
    graphql_reset_at=$(jq -r '.data.rateLimit.resetAt // empty' <<<"$res" 2>/dev/null)
    calls_to_api=$(get_BKG BKG_CALLS_TO_API)
    min_calls_to_api=$(get_BKG BKG_MIN_CALLS_TO_API)
    [ -n "$calls_to_api" ] || calls_to_api=0
    [ -n "$min_calls_to_api" ] || min_calls_to_api=0
    ((calls_to_api += graphql_cost))
    ((min_calls_to_api += graphql_cost))
    set_BKG BKG_CALLS_TO_API "$calls_to_api"
    set_BKG BKG_MIN_CALLS_TO_API "$min_calls_to_api"
    set_BKG BKG_GRAPHQL_LAST_COST "$graphql_cost"
    [[ "$graphql_remaining" =~ ^[0-9]+$ ]] && set_BKG BKG_GRAPHQL_REMAINING "$graphql_remaining"
    [ -n "$graphql_reset_at" ] && set_BKG BKG_GRAPHQL_RESET_AT "$graphql_reset_at"
    echo "$res"
}

graphql_escape_string() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

owner_ref_login() {
    [ -n "$1" ] || return

    if [[ "$1" =~ ^[1-9][0-9]*/.+$ ]]; then
        printf '%s\n' "${1#*/}"
    else
        printf '%s\n' "$1"
    fi
}

owner_id_cache_file() {
    if [ -n "${BKG_OWNER_ID_CACHE:-}" ]; then
        printf '%s\n' "$BKG_OWNER_ID_CACHE"
    elif [ -n "${BKG_ENV:-}" ]; then
        printf '%s\n' "$(dirname "$BKG_ENV")/owner-id-cache.txt"
    else
        printf '%s\n' "$BKG_ROOT/owner-id-cache.txt"
    fi
}

reset_owner_id_cache() {
    local cache_file

    cache_file=$(owner_id_cache_file) || return 1
    mkdir -p "$(dirname "$cache_file")" || return 1
    : >"$cache_file"
}

lookup_owner_ref_cache() {
    [ -n "$1" ] || return 1
    local owner_login
    local cache_file

    owner_login=$(owner_ref_login "$1") || return 1
    cache_file=$(owner_id_cache_file) || return 1
    [ -f "$cache_file" ] || return 1

    awk -F'/' -v owner_key="$owner_login" '
        $NF == owner_key {
            if (match_ref != "" && match_ref != $0) {
                conflict = 1
                exit
            }
            match_ref = $0
        }
        END {
            if (!conflict && match_ref != "") print match_ref
        }
    ' "$cache_file"
}

cache_owner_ref() {
    [ -n "$1" ] || return 0
    [[ "$1" =~ ^[1-9][0-9]*/.+$ ]] || return 0
    local owner_ref=$1
    local cache_file
    local cache_lock
    local owner_login
    local tmp_file

    cache_file=$(owner_id_cache_file) || return 1
    cache_lock="$cache_file.lock"
    owner_login=$(owner_ref_login "$owner_ref") || return 1
    mkdir -p "$(dirname "$cache_file")" || return 1
    [ -f "$cache_file" ] || : >"$cache_file"
    tmp_file=$(mktemp) || return 1

    until ln "$cache_file" "$cache_lock" 2>/dev/null; do sleep 0.05; done

    awk -F'/' -v owner_key="$owner_login" '$NF != owner_key' "$cache_file" >"$tmp_file"
    printf '%s\n' "$owner_ref" >>"$tmp_file"
    awk '!seen[$0]++' "$tmp_file" >"$tmp_file.dedup"
    mv "$tmp_file.dedup" "$tmp_file"
    mv "$tmp_file" "$cache_file"
    rm -f "$cache_lock"
}

graphql_owner_type() {
    [ -n "$1" ] || return
    local owner_login
    local response

    owner_login=$(owner_ref_login "$1") || return 1
    response=$(query_graphql_api "query { owner: repositoryOwner(login:\"$(graphql_escape_string "$owner_login")\") { __typename } }")
    (($? != 3)) || return 3
    jq -r '.data.owner.__typename // empty' <<<"$response"
}

graphql_discovery_reset_page_info() {
    GRAPHQL_DISCOVERY_HAS_NEXT_PAGE=false
    GRAPHQL_DISCOVERY_END_CURSOR=""
    GRAPHQL_DISCOVERY_NODES=""
}

graphql_repo_discovery_nodes() {
    [ -n "$1" ] || return 1
    [ -n "$2" ] || return 1
    local node=$1
    local edge=$2
    local cursor=${3:-}
    local owner
    local repo
    local after_arg=""
    local query
    local response
    local parsed_nodes=""

    owner=$(cut -d'/' -f1 <<<"$node")
    repo=$(cut -d'/' -f2- <<<"$node")
    [ -n "$owner" ] || return 1
    [ -n "$repo" ] || return 1
    graphql_discovery_reset_page_info
    [ -z "$cursor" ] || after_arg=", after:\"$(graphql_escape_string "$cursor")\""

    case "$edge" in
    stargazers|watchers)
        query="query { repository(owner:\"$(graphql_escape_string "$owner")\", name:\"$(graphql_escape_string "$repo")\") { $edge(first:100$after_arg) { nodes { login databaseId } pageInfo { hasNextPage endCursor } } } }"
        response=$(query_graphql_api "$query")
        (($? != 3)) || return 3
        GRAPHQL_DISCOVERY_HAS_NEXT_PAGE=$(jq -r ".data.repository.$edge.pageInfo.hasNextPage // false" <<<"$response" 2>/dev/null)
        GRAPHQL_DISCOVERY_END_CURSOR=$(jq -r ".data.repository.$edge.pageInfo.endCursor // empty" <<<"$response" 2>/dev/null)
        while IFS=$'\t' read -r owner_login owner_id; do
            [ -n "$owner_login" ] || continue
            [[ "$owner_id" =~ ^[1-9][0-9]*$ ]] || continue
            cache_owner_ref "$owner_id/$owner_login"
        done < <(jq -r ".data.repository.$edge.nodes[]? | select(.login != null and .databaseId != null) | \"\(.login)\t\(.databaseId)\"" <<<"$response" 2>/dev/null)
        parsed_nodes=$(jq -r ".data.repository.$edge.nodes[]? | select(.login != null) | .login" <<<"$response" 2>/dev/null)
        GRAPHQL_DISCOVERY_NODES=$parsed_nodes
        return 0
        ;;
    forks)
        query="query { repository(owner:\"$(graphql_escape_string "$owner")\", name:\"$(graphql_escape_string "$repo")\") { forks(first:100$after_arg) { nodes { owner { login ... on User { databaseId } ... on Organization { databaseId } } } pageInfo { hasNextPage endCursor } } } }"
        response=$(query_graphql_api "$query")
        (($? != 3)) || return 3
        GRAPHQL_DISCOVERY_HAS_NEXT_PAGE=$(jq -r '.data.repository.forks.pageInfo.hasNextPage // false' <<<"$response" 2>/dev/null)
        GRAPHQL_DISCOVERY_END_CURSOR=$(jq -r '.data.repository.forks.pageInfo.endCursor // empty' <<<"$response" 2>/dev/null)
        while IFS=$'\t' read -r owner_login owner_id; do
            [ -n "$owner_login" ] || continue
            [[ "$owner_id" =~ ^[1-9][0-9]*$ ]] || continue
            cache_owner_ref "$owner_id/$owner_login"
        done < <(jq -r '.data.repository.forks.nodes[]? | select(.owner.login != null and .owner.databaseId != null) | "\(.owner.login)\t\(.owner.databaseId)"' <<<"$response" 2>/dev/null)
        parsed_nodes=$(jq -r '.data.repository.forks.nodes[]? | .owner.login // empty' <<<"$response" 2>/dev/null)
        GRAPHQL_DISCOVERY_NODES=$parsed_nodes
        return 0
        ;;
    *)
        return 1
        ;;
    esac
}

graphql_owner_discovery_nodes() {
    [ -n "$1" ] || return 1
    [ -n "$2" ] || return 1
    local owner_ref=$1
    local edge=$2
    local cursor=${3:-}
    local owner_type=${4:-}
    local owner_login
    local after_arg=""
    local query
    local response
    local connection_name=""
    local parsed_nodes=""

    owner_login=$(owner_ref_login "$owner_ref") || return 1
    [ -n "$owner_type" ] || owner_type=$(graphql_owner_type "$owner_login")
    (($? != 3)) || return 3
    [ -n "$owner_type" ] || return 1
    graphql_discovery_reset_page_info
    [ -z "$cursor" ] || after_arg=", after:\"$(graphql_escape_string "$cursor")\""

    case "$edge" in
    followers|following|organizations)
        [ "$owner_type" = "User" ] || return 0
        connection_name=$edge
        query="query { owner: repositoryOwner(login:\"$(graphql_escape_string "$owner_login")\") { ... on User { $connection_name(first:100$after_arg) { nodes { login databaseId } pageInfo { hasNextPage endCursor } } } } }"
        response=$(query_graphql_api "$query")
        (($? != 3)) || return 3
        GRAPHQL_DISCOVERY_HAS_NEXT_PAGE=$(jq -r ".data.owner.$connection_name.pageInfo.hasNextPage // false" <<<"$response" 2>/dev/null)
        GRAPHQL_DISCOVERY_END_CURSOR=$(jq -r ".data.owner.$connection_name.pageInfo.endCursor // empty" <<<"$response" 2>/dev/null)
        while IFS=$'\t' read -r ref_login ref_id; do
            [ -n "$ref_login" ] || continue
            [[ "$ref_id" =~ ^[1-9][0-9]*$ ]] || continue
            cache_owner_ref "$ref_id/$ref_login"
        done < <(jq -r ".data.owner.$connection_name.nodes[]? | select(.login != null and .databaseId != null) | \"\(.login)\t\(.databaseId)\"" <<<"$response" 2>/dev/null)
        parsed_nodes=$(jq -r ".data.owner.$connection_name.nodes[]? | select(.login != null) | .login" <<<"$response" 2>/dev/null)
        GRAPHQL_DISCOVERY_NODES=$parsed_nodes
        return 0
        ;;
    people)
        [ "$owner_type" = "Organization" ] || return 0
        query="query { owner: repositoryOwner(login:\"$(graphql_escape_string "$owner_login")\") { ... on Organization { membersWithRole(first:100$after_arg) { nodes { login databaseId } pageInfo { hasNextPage endCursor } } } } }"
        response=$(query_graphql_api "$query")
        (($? != 3)) || return 3
        GRAPHQL_DISCOVERY_HAS_NEXT_PAGE=$(jq -r '.data.owner.membersWithRole.pageInfo.hasNextPage // false' <<<"$response" 2>/dev/null)
        GRAPHQL_DISCOVERY_END_CURSOR=$(jq -r '.data.owner.membersWithRole.pageInfo.endCursor // empty' <<<"$response" 2>/dev/null)
        while IFS=$'\t' read -r ref_login ref_id; do
            [ -n "$ref_login" ] || continue
            [[ "$ref_id" =~ ^[1-9][0-9]*$ ]] || continue
            cache_owner_ref "$ref_id/$ref_login"
        done < <(jq -r '.data.owner.membersWithRole.nodes[]? | select(.login != null and .databaseId != null) | "\(.login)\t\(.databaseId)"' <<<"$response" 2>/dev/null)
        parsed_nodes=$(jq -r '.data.owner.membersWithRole.nodes[]? | select(.login != null) | .login' <<<"$response" 2>/dev/null)
        GRAPHQL_DISCOVERY_NODES=$parsed_nodes
        return 0
        ;;
    *)
        return 1
        ;;
    esac
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
    local resolve_names=${2:-}
    local users
    users="$(curl "https://github.com/$1" | grep -oP 'href="/.+?".*>' | tr -d '\0' | grep -Ev '( .*|\?(return_to|tab))=' | tr -d '\0' | grep -oP '/.*?"' | cut -c2- | rev | cut -c2- | rev | grep -v "/")"
    (($? != 3)) || return 3
    [ -z "$resolve_names" ] && echo "$users" || get_owners "$users"
}

curl_orgs() {
    local target=$1
    local resolve_names=${2:-}
    local owner_type=""
    local cursor=""
    local orgs=""
    local status=0

    if [ -n "${GITHUB_TOKEN:-}" ] && [[ "$target" != orgs/* ]] && [[ "$target" != *\?* ]] && [[ "$target" != */*/* ]]; then
        owner_type=$(graphql_owner_type "$target")
        status=$?
        ((status != 3)) || return 3

        if [ "$owner_type" = "User" ]; then
            while true; do
                graphql_owner_discovery_nodes "$target" organizations "$cursor" "$owner_type"
                status=$?
                ((status != 3)) || return 3
                ((status == 0)) || break
                orgs=$GRAPHQL_DISCOVERY_NODES
                [ -z "$resolve_names" ] && echo "$orgs" || get_owners "$orgs"
                [ "$GRAPHQL_DISCOVERY_HAS_NEXT_PAGE" = "true" ] || return 0
                cursor=$GRAPHQL_DISCOVERY_END_CURSOR
            done
        elif [ "$owner_type" = "Organization" ]; then
            return 0
        fi
    fi

    if [[ "$target" =~ ^[1-9][0-9]*/[^/?]+$ ]]; then
        target=${target#*/}
    fi

    orgs="$(curl "https://github.com/$target" | grep -oP '/orgs/[^/]+' | tr -d '\0' | cut -d'/' -f3)"
    (($? != 3)) || return 3
    [ -z "$resolve_names" ] && echo "$orgs" || get_owners "$orgs"
}

explore() {
    local node=$1
	local is_repo=false
	local is_user=false
	local got_orgs=false
	local status=0
    local graphql_owner_type=""
	[[ ! "$node" =~ .*\/.* ]] || is_repo=true
    [ "$is_repo" = true ] && local graph=("stargazers" "watchers" "forks" "collaborators") || local graph=("followers" "following" "people")
    [ -z "$2" ] || graph=("$2")

    if [ "$is_repo" = false ] && [ -n "${GITHUB_TOKEN:-}" ]; then
        graphql_owner_type=$(graphql_owner_type "$node")
        status=$?
        ((status != 3)) || return 3
    fi

    for edge in "${graph[@]}"; do
        local page=1
        local cursor=""
        while true; do
            local nodes

            if [ -n "${GITHUB_TOKEN:-}" ] && [ "$edge" != "collaborators" ]; then
                if [ "$is_repo" = true ]; then
                    graphql_repo_discovery_nodes "$node" "$edge" "$cursor"
                    status=$?
                    ((status != 3)) || return 3

                    if ((status == 0)); then
                        nodes=$GRAPHQL_DISCOVERY_NODES
                        grep -v "$(cut -d'/' -f1 <<<"$node")" <<<"$nodes"
                        [ "$GRAPHQL_DISCOVERY_HAS_NEXT_PAGE" = "true" ] || break
                        cursor=$GRAPHQL_DISCOVERY_END_CURSOR
                        continue
                    fi
                elif [ -n "$graphql_owner_type" ]; then
                    graphql_owner_discovery_nodes "$node" "$edge" "$cursor" "$graphql_owner_type"
                    status=$?
                    ((status != 3)) || return 3

                    if ((status == 0)); then
                        nodes=$GRAPHQL_DISCOVERY_NODES
                        grep -v "$(cut -d'/' -f1 <<<"$node")" <<<"$nodes"

                        if [ "$graphql_owner_type" = "User" ] && [ "$got_orgs" = false ]; then
                            curl_orgs "$node"
                            status=$?
                            ((status != 3)) || return 3
                            got_orgs=true
                        fi

                        [ "$GRAPHQL_DISCOVERY_HAS_NEXT_PAGE" = "true" ] || break
                        cursor=$GRAPHQL_DISCOVERY_END_CURSOR
                        continue
                    fi
                fi
            fi

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
    local owner_type=""
    local people=""
    local cursor=""
    local status=0
    owner=$(cut -d'/' -f2 <<<"$1")

    if [ -n "${GITHUB_TOKEN:-}" ]; then
        owner_type=$(graphql_owner_type "$owner")
        status=$?
        ((status != 3)) || return 3

        if [ "$owner_type" = "Organization" ]; then
            while true; do
                graphql_owner_discovery_nodes "$owner" people "$cursor" "$owner_type"
                status=$?
                ((status != 3)) || return 3
                ((status == 0)) || break
                people=$GRAPHQL_DISCOVERY_NODES
                echo "$people"
                [ "$GRAPHQL_DISCOVERY_HAS_NEXT_PAGE" = "true" ] || return 0
                cursor=$GRAPHQL_DISCOVERY_END_CURSOR
            done
        elif [ "$owner_type" = "User" ]; then
            curl_orgs "$owner"
            return $?
        fi
    fi

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
