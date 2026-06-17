#!/bin/bash
# Backage library
# Usage: ./lib.sh
# Dependencies: git curl jq parallel python3 python3-httpx sqlite3 zstd libxml2-utils
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

if [ -z "${BKG_UTIL_BOOTSTRAPPED:-}" ]; then
    if [ "${BKG_SKIP_DEP_VERIFY:-0}" != "1" ]; then
        echo "Verifying dependencies..."
        apt_install git curl jq parallel python3 python3-httpx sqlite3 zstd libxml2-utils
        echo "Dependencies verified!"
    fi

    BKG_UTIL_BOOTSTRAPPED=1
fi
GITHUB_OWNER=${GITHUB_OWNER:-ipitio}
GITHUB_REPO=${GITHUB_REPO:-backage}
BKG_ROOT="$(
    cd -P "$(dirname "${BASH_SOURCE[0]}")" &&
        cd ../.. &&
        pwd -P
)"
BKG_ENV=${BKG_ENV:-$BKG_ROOT/src/env.env}
BKG_OWNERS=${BKG_OWNERS:-$BKG_ROOT/owners.txt}
BKG_OPTOUT=${BKG_OPTOUT:-$BKG_ROOT/optout.txt}
BKG_INDEX_TBL_OWN=${BKG_INDEX_TBL_OWN:-owners}
BKG_INDEX_TBL_PKG=${BKG_INDEX_TBL_PKG:-packages}
BKG_INDEX_TBL_VER=${BKG_INDEX_TBL_VER:-versions}
BKG_MODE=${BKG_MODE:-0}
BKG_MAX_LEN=${BKG_MAX_LEN:-14400}
BKG_IS_FIRST=${BKG_IS_FIRST:-false}
BKG_PAGE_ALL=${BKG_PAGE_ALL:-1}
BKG_OWNER_NOT_FOUND_STATUS=4
BKG_OWNER_RETRY_INITIAL_SECONDS=${BKG_OWNER_RETRY_INITIAL_SECONDS:-3600}
BKG_OWNER_RETRY_MAX_SECONDS=${BKG_OWNER_RETRY_MAX_SECONDS:-86400}

# format numbers like 1000 to 1k
numfmt() {
    awk '{ split("k M B T P E Z Y", v); s=0; while( $1>999.9 ) { $1/=1000; s++ } print int($1*10)/10 v[s] }'
}

# format bytes to KB, MB, GB, etc.
numfmt_size() {
    # use sed to remove trailing \s*$
    awk '{ split("kB MB GB TB PB EB ZB YB", v); s=0; while( $1>999.9 ) { $1/=1000; s++ } print int($1*10)/10 " " v[s] }' | sed 's/[[:blank:]]*$//'
}

fmtmetric_num() {
    awk '
    {
        value = $0
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
        gsub(/,/, "", value)
        if (value == "") next

        suffix = substr(value, length(value), 1)
        power = 0
        if (suffix ~ /[[:alpha:]]/) {
            value = substr(value, 1, length(value) - 1)
            suffix = toupper(suffix)
            power = index("KMBTPEZY", suffix)
            if (power == 0) next
        }

        if (value !~ /^[0-9]+(\.[0-9]+)?$/) next
        printf "%.0f", value * (1000 ^ power)
    }'
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
    bkg_python snapshot path db
}

legacy_db_snapshot_archive_file() {
    bkg_python snapshot path db-zst
}

legacy_sql_snapshot_archive_file() {
    bkg_python snapshot path sql-zst
}

db_snapshot_asset_name() {
    bkg_python snapshot asset-name db
}

legacy_db_snapshot_asset_name() {
    bkg_python snapshot asset-name db-zst
}

legacy_sql_snapshot_asset_name() {
    bkg_python snapshot asset-name sql-zst
}

resolve_release_snapshot_asset() {
    local latest=$1
    local db_asset_name
    local legacy_db_asset_name
    local legacy_asset_name
    local status_code

    db_asset_name=$(db_snapshot_asset_name 2>/dev/null || echo "index.db")
    status_code=$(curl -o /dev/null --silent -Iw '%{http_code}' "https://github.com/${GITHUB_OWNER}/${GITHUB_REPO}/releases/download/$latest/$db_asset_name")
    if [[ "$status_code" =~ ^[23][0-9][0-9]$ ]]; then
        printf 'db|%s\n' "$db_asset_name"
        return 0
    fi

    legacy_db_asset_name=$(legacy_db_snapshot_asset_name 2>/dev/null || echo "index.db.zst")
    status_code=$(curl -o /dev/null --silent -Iw '%{http_code}' "https://github.com/${GITHUB_OWNER}/${GITHUB_REPO}/releases/download/$latest/$legacy_db_asset_name")
    if [[ "$status_code" =~ ^[23][0-9][0-9]$ ]]; then
        printf 'db-zst|%s\n' "$legacy_db_asset_name"
        return 0
    fi

    legacy_asset_name=$(legacy_sql_snapshot_asset_name 2>/dev/null || echo "index.sql.zst")
    status_code=$(curl -o /dev/null --silent -Iw '%{http_code}' "https://github.com/${GITHUB_OWNER}/${GITHUB_REPO}/releases/download/$latest/$legacy_asset_name")
    if [[ "$status_code" =~ ^[23][0-9][0-9]$ ]]; then
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

index_sparse_add_paths() {
    index_worktree_is_git_repo || return 0
    local path
    local -a batch=()

    while IFS= read -r path; do
        [ -n "$path" ] || continue
        batch+=("$path")

        if ((${#batch[@]} >= 100)); then
            git -C "$BKG_INDEX_DIR" sparse-checkout add --skip-checks -- "${batch[@]}" || return 1
            batch=()
        fi
    done

    if ((${#batch[@]} > 0)); then
        git -C "$BKG_INDEX_DIR" sparse-checkout add --skip-checks -- "${batch[@]}" || return 1
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

sqlite_quote_literal() {
    printf "'%s'" "$(sqlite_escape_literal "$1")"
}

sqlite_quote_identifier() {
    printf '"%s"' "$(sqlite_escape_identifier "$1")"
}

bkg_python() {
    local -a python_env=(
        "GITHUB_OWNER=$GITHUB_OWNER"
        "GITHUB_REPO=$GITHUB_REPO"
        "GITHUB_TOKEN=${GITHUB_TOKEN:-}"
        "BKG_ROOT=$BKG_ROOT"
        "BKG_ENV=$BKG_ENV"
        "BKG_OWNERS=$BKG_OWNERS"
        "BKG_OPTOUT=$BKG_OPTOUT"
        "BKG_MODE=$BKG_MODE"
        "BKG_MAX_LEN=$BKG_MAX_LEN"
        "BKG_IS_FIRST=$BKG_IS_FIRST"
        "BKG_PAGE_ALL=$BKG_PAGE_ALL"
        "BKG_INDEX_DB=${BKG_INDEX_DB:-}"
        "BKG_INDEX_TBL_OWN=$BKG_INDEX_TBL_OWN"
        "BKG_INDEX_TBL_PKG=$BKG_INDEX_TBL_PKG"
        "BKG_INDEX_TBL_VER=$BKG_INDEX_TBL_VER"
        "BKG_SQLITE_BUSY_TIMEOUT_MS=${BKG_SQLITE_BUSY_TIMEOUT_MS:-300000}"
        "BKG_SQLITE_MAX_ATTEMPTS=${BKG_SQLITE_MAX_ATTEMPTS:-3}"
        "BKG_SQLITE_RETRY_DELAY_SECS=${BKG_SQLITE_RETRY_DELAY_SECS:-1}"
        "BKG_OWNER_RETRY_INITIAL_SECONDS=$BKG_OWNER_RETRY_INITIAL_SECONDS"
        "BKG_OWNER_RETRY_MAX_SECONDS=$BKG_OWNER_RETRY_MAX_SECONDS"
        "PYTHONDONTWRITEBYTECODE=1"
        "PYTHONPATH=$BKG_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
    )
    local name
    local python_bin=${BKG_PYTHON:-}

    if [ -z "$python_bin" ] && [ -x "$BKG_ROOT/.venv/bin/python" ]; then
        python_bin="$BKG_ROOT/.venv/bin/python"
    fi
    [ -n "$python_bin" ] || python_bin=python3

    for name in \
        GITHUB_BRANCH \
        BKG_INDEX \
        BKG_INDEX_SQL \
        BKG_INDEX_DIR \
        BKG_GITHUB_API_URL \
        BKG_HTTP_CONNECT_TIMEOUT \
        BKG_HTTP_READ_TIMEOUT \
        BKG_HTTP_WRITE_TIMEOUT \
        BKG_HTTP_POOL_TIMEOUT \
        BKG_HTTP_TOTAL_TIMEOUT \
        BKG_HTTP_MAX_ATTEMPTS \
        BKG_HTTP_INITIAL_BACKOFF \
        BKG_HTTP_MAX_BACKOFF \
        BKG_HTTP_USER_AGENT \
        BKG_OWNER_ID_CACHE \
        BKG_OWNER_ARRAY_VERSION_LIMIT \
        BKG_OWNER_ARRAY_MAX_BYTES \
        BKG_OWNER_ARRAY_ADAPTIVE_MAX_PROBE \
        BKG_OWNER_ARRAY_DB_ESTIMATE_HEADROOM_PERCENT \
        BKG_OWNER_ARRAY_DB_FALLBACK_VERSION_LIMIT \
        BKG_OWNER_ARRAY_DB_VERSION_LIMIT; do
        [ -z "${!name+x}" ] || python_env+=("$name=${!name}")
    done

    env "${python_env[@]}" "$python_bin" -m bkg_py "$@"
}

sqlite_ensure_index_schema() {
    [ -n "${BKG_INDEX_DB:-}" ] || return 1
    local schema_key="${BKG_INDEX_DB}|${BKG_INDEX_TBL_OWN}|${BKG_INDEX_TBL_PKG}|${BKG_INDEX_TBL_VER}"

    [ "${BKG_INDEX_SCHEMA_READY_FOR:-}" != "$schema_key" ] || return 0

    bkg_python database ensure-schema || return $?
    BKG_INDEX_SCHEMA_READY_FOR=$schema_key
}

cleanup_generated_json_sidecars() {
    [ -n "$1" ] || return
    [ -e "$1" ] || return 0

    find "$1" -type f \( \
        -name '*.json.tmp' -o -name '*.json.tmp.*' -o \
        -name '*.json.abs' -o -name '*.json.abs.*' -o \
        -name '*.json.rel' -o -name '*.json.rel.*' \
    \) -delete
}

ytoxt_script_path() {
    if [ -f "$BKG_ROOT/src/lib/ytoxt.sh" ]; then
        printf '%s\n' "$BKG_ROOT/src/lib/ytoxt.sh"
    else
        printf '%s\n' "lib/ytoxt.sh"
    fi
}

get_BKG() {
    [ -f "$BKG_ENV" ] || return
    while [ -f "$BKG_ENV.lock" ]; do sleep 0.05; done
    grep "^$1=" "$BKG_ENV" 2>/dev/null | cut -d'=' -f2
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

current_batch_first_started() {
    local batch_first_started

    batch_first_started=$(get_BKG BKG_BATCH_FIRST_STARTED)
    [ -n "$batch_first_started" ] || batch_first_started="${BKG_BATCH_FIRST_STARTED:-}"
    printf '%s\n' "$batch_first_started"
}

ensure_pages_dotfiles_visible() {
    [ -n "${1:-}" ] || return 1
    mkdir -p "$1" || return 1
    : >"$1/.nojekyll" || return 1
}

daily_gate_completed_today() {
    [ -n "$1" ] || return 1
    local today_value=${2:-$(date -u +%Y-%m-%d)}
    [ "$(get_BKG "$1")" = "$(daily_gate_state_value "$today_value")" ]
}

generate_batch_marker() {
    printf '%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)-$$"
}

daily_gate_batch_marker() {
    local marker

    marker=$(get_BKG BKG_BATCH_MARKER)
    [ -n "$marker" ] || marker="${BKG_BATCH_FIRST_STARTED:-}"
    [ -n "$marker" ] || marker="default"
    printf '%s\n' "$marker"
}

daily_gate_rest_to_top() {
    local rest_to_top

    rest_to_top=$(get_BKG BKG_REST_TO_TOP)
    [ -n "$rest_to_top" ] || rest_to_top="${BKG_REST_TO_TOP:-0}"
    [ -n "$rest_to_top" ] || rest_to_top="0"
    printf '%s\n' "$rest_to_top"
}

daily_gate_state_value() {
    local today_value=${1:-$(date -u +%Y-%m-%d)}
    printf '%s|%s|%s\n' "$today_value" "$(daily_gate_batch_marker)" "$(daily_gate_rest_to_top)"
}

master_branch_has_commit_today() {
    local today_value=${1:-$(date -u +%Y-%m-%d)}
    local today_start_epoch=""
    local master_commit_epoch=""

    git rev-parse --verify master >/dev/null 2>&1 || return 1
    today_start_epoch=$(date -u -d "$today_value 00:00:00" +%s 2>/dev/null) || return 1
    master_commit_epoch=$(git log -1 --format=%ct master 2>/dev/null) || return 1
    [[ "$master_commit_epoch" =~ ^[0-9]+$ ]] || return 1
    ((master_commit_epoch >= today_start_epoch))
}

daily_gate_should_skip_today() {
    [ -n "$1" ] || return 1
    local today_value=${2:-$(date -u +%Y-%m-%d)}

    daily_gate_completed_today "$1" "$today_value" || return 1
    master_branch_has_commit_today "$today_value"
}

mark_daily_gate_completed() {
    [ -n "$1" ] || return 1
    local today_value=${2:-$(date -u +%Y-%m-%d)}
    set_BKG "$1" "$(daily_gate_state_value "$today_value")"
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

check_script_timeout() {
    local rate_limit_end
    local rate_limit_start
    local script_limit_diff

    ((BKG_MAX_LEN > 0)) || return 0
    rate_limit_end=$(date -u +%s)
    rate_limit_start=$(get_BKG BKG_SCRIPT_START)

    if [ -z "$rate_limit_start" ]; then
        sleep 0.05
        rate_limit_start=$(get_BKG BKG_SCRIPT_START)
    fi

    [ -n "$rate_limit_start" ] || rate_limit_start="${BKG_SCRIPT_START:-}"
    [ -n "$rate_limit_start" ] || {
        echo "BKG_SCRIPT_START empty!"
        return 0
    }

    script_limit_diff=$((rate_limit_end - rate_limit_start))
    CHECK_LIMIT_SCRIPT_START=$rate_limit_start
    CHECK_LIMIT_SCRIPT_DIFF=$script_limit_diff
    ((script_limit_diff < BKG_MAX_LEN)) || save_and_exit
    (($? != 3)) || return 3
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
        check_script_timeout
        status=$?

        if ((status == 3)) || stop_requested; then
            terminate_process_tree "$pid"
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

run_command_to_file_with_stop_check() {
    local output_file=$1
    local stderr_file
    local pid
    local status

    [ -n "$output_file" ] || return 1
    shift

    stderr_file=$(mktemp)

    "$@" >"$output_file" 2>"$stderr_file" &
    pid=$!

    while kill -0 "$pid" 2>/dev/null; do
        check_script_timeout
        status=$?

        if ((status == 3)) || stop_requested; then
            terminate_process_tree "$pid"
            rm -f "$stderr_file"
            return 3
        fi

        sleep 1
    done

    wait "$pid"
    status=$?
    cat "$stderr_file" >&2

    rm -f "$stderr_file"
    return "$status"
}

collect_child_pids() {
    local root_pid=$1
    local child_pid

    [ -n "$root_pid" ] || return

    while IFS= read -r child_pid; do
        child_pid=$(awk '{print $1}' <<<"$child_pid")
        [ -n "$child_pid" ] || continue
        printf '%s\n' "$child_pid"
        collect_child_pids "$child_pid"
    done < <(ps -o pid= --ppid "$root_pid" 2>/dev/null)
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

terminate_process_tree() {
    local root_pid
    local pid
    local -a pids=()

    (("$#" > 0)) || return

    for root_pid in "$@"; do
        [ -n "$root_pid" ] || continue
        while IFS= read -r pid; do
            [ -n "$pid" ] || continue
            pids+=("$pid")
        done < <(collect_child_pids "$root_pid")

        pids+=("$root_pid")
    done

    ((${#pids[@]} > 0)) || return
    terminate_pids_with_grace "${pids[@]}"
}

script_stop_requested() {
    local timeout_status=0

    stop_requested && return 0
    [ -n "$(get_BKG BKG_SCRIPT_START)" ] || [ -n "${BKG_SCRIPT_START:-}" ] || return 1
    check_script_timeout
    timeout_status=$?
    ((timeout_status == 3)) && return 0
    stop_requested
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
    local rate_limit_diff
    local hours_passed
    local remaining_time
    local minute_calls
    local sec_limit_diff
    local min_passed
    local rate_limit_start
    local script_limit_diff

    check_script_timeout || return $?
    rate_limit_end=$(date -u +%s)
    rate_limit_start="${CHECK_LIMIT_SCRIPT_START:-${BKG_SCRIPT_START:-$rate_limit_end}}"
    script_limit_diff=${CHECK_LIMIT_SCRIPT_DIFF:-$((rate_limit_end - rate_limit_start))}
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

    check_limit || return $?

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

        if grep -q "3" <<<"$code" || script_stop_requested; then
            printf '%s\n' 3 >>"$exit_code"
            terminate_process_tree "${active_pids[@]}"
            active_pids=()
            stop_now=true
            break
        fi

        grep -q "2" <<<"$code" && break

        while ((${#active_pids[@]} >= max_jobs)); do
            mapfile -t active_pids < <(filter_running_pids "${active_pids[@]}")
            code=$(cat "$exit_code")

            if grep -q "3" <<<"$code" || script_stop_requested; then
                printf '%s\n' 3 >>"$exit_code"
                terminate_process_tree "${active_pids[@]}"
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

        if grep -q "3" <<<"$code" || script_stop_requested; then
            printf '%s\n' 3 >>"$exit_code"
            terminate_process_tree "${active_pids[@]}"
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
    local stdin_file
    local parallel_pid
    shift 2

    stderr_file=$(mktemp)
    stdin_file=$(mktemp)
    cat >"$stdin_file"
    parallel "$@" bash "$BKG_ROOT/src/lib/parallel-worker.sh" "$source_file" "$function_name" <"$stdin_file" 2>"$stderr_file" &
    parallel_pid=$!

    while kill -0 "$parallel_pid" 2>/dev/null; do
        if script_stop_requested; then
            terminate_process_tree "$parallel_pid"
            status=3
            break
        fi

        sleep 1
    done

    if [ -z "${status:-}" ]; then
        wait "$parallel_pid"
        status=$?
    fi

    if ((status == 2 || status == 3)) && [ "$(get_BKG BKG_TIMEOUT)" = "1" ]; then
        grep -Ev '^parallel: This job failed:$|^bash .*/parallel-worker\.sh .*$|^parallel: Starting no more jobs\. Waiting for [0-9]+ jobs to finish\.$' "$stderr_file" >&2 || :
        rm -f "$stderr_file" "$stdin_file"
        return 3
    fi

    cat "$stderr_file" >&2
    rm -f "$stderr_file" "$stdin_file"
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
            terminate_process_tree "${PARALLEL_ASYNC_PIDS[@]}"
            PARALLEL_ASYNC_PIDS=()
            PARALLEL_ASYNC_RUNNING=0
            return "$async_status"
        }

        if ((PARALLEL_ASYNC_LAST_STATUS != 0)); then
            :
        fi

        if script_stop_requested; then
            printf '%s\n' 3 >>"$PARALLEL_ASYNC_EXIT_CODE"
            terminate_process_tree "${PARALLEL_ASYNC_PIDS[@]}"
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

        if script_stop_requested; then
            printf '%s\n' 3 >>"$PARALLEL_ASYNC_EXIT_CODE"
            terminate_process_tree "${PARALLEL_ASYNC_PIDS[@]}"
            PARALLEL_ASYNC_PIDS=()
            PARALLEL_ASYNC_RUNNING=0
            status=3
            break
        fi

        parallel_async_status || {
            async_status=$?
            status=$async_status
            terminate_process_tree "${PARALLEL_ASYNC_PIDS[@]}"
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
    local latest=${1:-}
    local -a args=(snapshot download-release)
    local status

    [ -z "$latest" ] || args+=("$latest")
    [ -z "${2:-}" ] || args+=("--check")
    if [ -n "${2:-}" ]; then
        bkg_python "${args[@]}" >/dev/null
        return $?
    fi
    echo "Downloading the latest database..."
    # `cd src ; source bkg.sh && dldb` to dl the latest db
    bkg_python "${args[@]}"
    status=$?
    if ((status != 0)); then
        echo "Failed to get the latest database"
    fi

    [ -f "$BKG_ROOT/.gitignore" ] || echo "*.db*" >>"$BKG_ROOT/.gitignore"
    grep -q "\*.db" "$BKG_ROOT/.gitignore" || echo "*.db*" >>"$BKG_ROOT/.gitignore"
    return "$status"
}

curl_gh_direct() {
    command curl -H "Accept: application/vnd.github+json" -H "Authorization: Bearer $GITHUB_TOKEN" -H "X-GitHub-Api-Version: 2022-11-28" "$@"
}

query_api() {
    check_limit || return $?
    bkg_python github rest "$1"
}

query_api_optional() {
    check_limit || return $?
    bkg_python github rest "$1" --missing-ok
}

query_graphql_api() {
    local query=$1

    check_limit || return $?
    printf '%s' "$query" | bkg_python github graphql
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
    bkg_python discovery owner-type "$1"
}

graphql_discovery_reset_page_info() {
    GRAPHQL_DISCOVERY_HAS_NEXT_PAGE=false
    GRAPHQL_DISCOVERY_END_CURSOR=""
    GRAPHQL_DISCOVERY_NODES=""
}

graphql_discovery_read_page() {
    local output=$1
    local key
    local value
    local nodes=""

    graphql_discovery_reset_page_info
    while IFS=$'\t' read -r key value; do
        case "$key" in
        has_next)
            GRAPHQL_DISCOVERY_HAS_NEXT_PAGE=$value
            ;;
        end_cursor)
            GRAPHQL_DISCOVERY_END_CURSOR=$value
            ;;
        node)
            nodes="${nodes:+$nodes$'\n'}$value"
            ;;
        esac
    done <<<"$output"
    GRAPHQL_DISCOVERY_NODES=$nodes
}

graphql_repo_discovery_nodes() {
    [ -n "$1" ] || return 1
    [ -n "$2" ] || return 1
    local node=$1
    local edge=$2
    local cursor=${3:-}
    local owner
    local repo
    local output
    local status=0

    owner=$(cut -d'/' -f1 <<<"$node")
    repo=$(cut -d'/' -f2- <<<"$node")
    [ -n "$owner" ] || return 1
    [ -n "$repo" ] || return 1
    graphql_discovery_reset_page_info

    output=$(bkg_python discovery repo-nodes "$owner" "$repo" "$edge" "$cursor") || status=$?
    ((status != 3)) || return 3
    ((status == 0)) || return "$status"
    graphql_discovery_read_page "$output"
}

graphql_owner_discovery_nodes() {
    [ -n "$1" ] || return 1
    [ -n "$2" ] || return 1
    local owner_ref=$1
    local edge=$2
    local cursor=${3:-}
    local owner_type=${4:-}
    local owner_login
    local output
    local status=0

    owner_login=$(owner_ref_login "$owner_ref") || return 1
    [ -n "$owner_type" ] || owner_type=$(graphql_owner_type "$owner_login")
    status=$?
    ((status != 3)) || return 3
    ((status == 0)) || return "$status"
    [ -n "$owner_type" ] || return 1
    graphql_discovery_reset_page_info

    output=$(bkg_python discovery owner-nodes "$owner_login" "$edge" "$cursor" "$owner_type") || status=$?
    ((status != 3)) || return 3
    ((status == 0)) || return "$status"
    graphql_discovery_read_page "$output"
}

release_has_snapshot_asset() {
    local release=$1
    local db_asset_name
    local legacy_db_asset_name
    local legacy_sql_asset_name

    db_asset_name=$(db_snapshot_asset_name 2>/dev/null || echo "index.db")
    legacy_db_asset_name=$(legacy_db_snapshot_asset_name 2>/dev/null || echo "index.db.zst")
    legacy_sql_asset_name=$(legacy_sql_snapshot_asset_name 2>/dev/null || echo "index.sql.zst")

    jq -e \
        --arg db "$db_asset_name" \
        --arg legacy_db "$legacy_db_asset_name" \
        --arg legacy_sql "$legacy_sql_asset_name" \
        'any(.assets[]?; .name == $db or .name == $legacy_db or .name == $legacy_sql)' \
        <<<"$release" >/dev/null 2>&1
}

check_db() {
    local release
    local release_id
    local latest

    while true; do
        release=$(curl_gh_direct --fail-with-body --silent --show-error "https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/releases/latest") || {
            echo "Failed to get the latest release metadata" >&2
            return 1
        }
        release_id=$(jq -er '.id | select(type == "number" and . > 0)' <<<"$release" 2>/dev/null) || {
            echo "Latest release metadata has no valid release ID" >&2
            return 1
        }
        latest=$(jq -er '.tag_name | select(type == "string" and length > 0)' <<<"$release" 2>/dev/null) || {
            echo "Latest release metadata has no valid tag" >&2
            return 1
        }

        release_has_snapshot_asset "$release" && return 0

        echo "Deleting the latest release..."
        curl_gh_direct --fail-with-body --silent --show-error --output /dev/null -X DELETE "https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/releases/$release_id" || {
            echo "Failed to delete latest release $latest" >&2
            return 1
        }
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
    local owner_ref
    local status=0

    owner_ref=$(bkg_python discovery resolve-owner "$1")
    status=$?
    ((status != 3)) || return 3
    ((status == 0)) || return "$status"
    [ -n "$owner_ref" ] || return "$BKG_OWNER_NOT_FOUND_STATUS"
    printf '%s\n' "$owner_ref"
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

print_nonempty_lines() {
    [ -z "$1" ] || printf '%s\n' "$1"
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
        if [ -n "$resolve_names" ]; then
            orgs=$(bkg_python discovery orgs "$target" --resolve) || status=$?
        else
            orgs=$(bkg_python discovery orgs "$target") || status=$?
        fi
        ((status != 3)) || return 3
        if ((status == 0)); then
            print_nonempty_lines "$orgs"
            return 0
        fi

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
    local nodes=""
    local graphql_owner_type=""
	[[ ! "$node" =~ .*\/.* ]] || is_repo=true
    [ "$is_repo" = true ] && local graph=("stargazers" "watchers" "forks" "collaborators") || local graph=("followers" "following" "people")
    [ -z "$2" ] || graph=("$2")

    if [ -n "${GITHUB_TOKEN:-}" ]; then
        if [ -n "${2:-}" ]; then
            nodes=$(bkg_python discovery explore "$node" "$2") || status=$?
        else
            nodes=$(bkg_python discovery explore "$node") || status=$?
        fi
        ((status != 3)) || return 3
        if ((status == 0)); then
            print_nonempty_lines "$nodes"
            return 0
        fi
    fi

    if [ "$is_repo" = false ] && [ -n "${GITHUB_TOKEN:-}" ]; then
        graphql_owner_type=$(graphql_owner_type "$node")
        status=$?
        ((status != 3)) || return 3
    fi

    for edge in "${graph[@]}"; do
        local page=1
        local cursor=""
        while true; do
            nodes=""

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
        people=$(bkg_python discovery membership "$1") || status=$?
        ((status != 3)) || return 3
        if ((status == 0)); then
            print_nonempty_lines "$people"
            return 0
        fi

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
    bkg_python json-to-xml "$1"
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
