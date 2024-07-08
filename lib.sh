#!/bin/bash
# Setup the environment
# Usage: ./lib.sh
# Dependencies: curl
# Copyright (c) ipitio
#
# shellcheck disable=SC1091,SC2015,SC2034

source .env
declare SCRIPT_START
declare TODAY
SCRIPT_START=$(date +%s)
TODAY=$(date -u +%Y-%m-%d)
readonly SCRIPT_START TODAY

if ! command -v curl &>/dev/null || ! command -v jq &>/dev/null || ! command -v sqlite3 &>/dev/null; then
    echo "Installing dependencies..."
    sudo apt-get update
    sudo apt-get install curl jq sqlite3 -y
fi

# format numbers like 1000 to 1k
numfmt() {
    awk '{ split("k M B T P E Z Y", v); s=0; while( $1>999.9 ) { $1/=1000; s++ } print int($1*10)/10 v[s] }'
}

# format bytes to KB, MB, GB, etc.
numfmt_size() {
    awk '{ split("kB MB GB TB PB EB ZB YB", v); s=0; while( $1>999.9 ) { $1/=1000; s++ } print int($1*10)/10 " " v[s] }'
}

curl() {
    # if connection times out or max time is reached, wait increasing amounts of time before retrying
    local i=0
    local max_attempts=7
    local wait_time=1
    local result

    while [ "$i" -lt "$max_attempts" ]; do
        result=$(command curl -sSLNZ --connect-timeout 60 -m 120 "$@" 2>/dev/null)
        [ -n "$result" ] && echo "$result" && return 0
        sleep "$wait_time"
        ((i++))
        ((wait_time *= 2))
    done

    return 1
}

xz_db() {
    echo "Compressing the database..."
    sqlite3 "$BKG_INDEX_DB" ".dump" | tar -c -I 'zstd -22 --ultra --long -T0' "$BKG_INDEX_SQL".tar.zst.new

    if [ -f "$BKG_INDEX_SQL".tar.zst.new ]; then
        # rotate the database if it's greater than 2GB
        if [ "$(stat -c %s "$BKG_INDEX_SQL".tar.zst.new)" -ge 2000000000 ]; then
            echo "Rotating the database..."
            [ -d "$BKG_INDEX_SQL".d ] || mkdir "$BKG_INDEX_SQL".d
            [ ! -f "$BKG_INDEX_SQL".tar.zst ] || mv "$BKG_INDEX_SQL".tar.zst "$BKG_INDEX_SQL".d/"$(date -u +%Y.%m.%d)".tar.zst
            query="delete from '$BKG_INDEX_TBL_PKG' where date not between date('$BKG_BATCH_FIRST_STARTED') and date('$TODAY');"
            sqlite3 "$BKG_INDEX_DB" "$query"
            query="select name from sqlite_master where type='table' and name like '${BKG_INDEX_TBL_VER}_%';"
            tables=$(sqlite3 "$BKG_INDEX_DB" "$query")

            for table in $tables; do
                query="delete from '$table' where date not between date('$BKG_BATCH_FIRST_STARTED') and date('$TODAY');"
                sqlite3 "$BKG_INDEX_DB" "$query"
            done

            sqlite3 "$BKG_INDEX_DB" "vacuum;"
            sqlite3 "$BKG_INDEX_DB" ".dump" | tar -c -I 'zstd -22 --ultra --long -T0' "$BKG_INDEX_SQL".tar.zst.new
        fi

        mv "$BKG_INDEX_SQL".tar.zst.new "$BKG_INDEX_SQL".tar.zst
    else
        echo "Failed to compress the database!"
    fi

    echo "Exiting..."
    env | grep -E '^BKG_' >.env
    exit 2
}

check_limit() {
    # exit if the script has been running for 5 hours
    rate_limit_end=$(date +%s)
    script_limit_diff=$((rate_limit_end - SCRIPT_START))
    ((script_limit_diff < 18000)) || echo "Script has been running for 5 hours!" && exit 0

    # wait if 1000 or more calls have been made in the last hour
    rate_limit_diff=$((rate_limit_end - BKG_RATE_LIMIT_START))
    hours_passed=$((rate_limit_diff / 3600))

    if ((BKG_CALLS_TO_API >= 1000 * (hours_passed + 1))); then
        echo "$BKG_CALLS_TO_API calls to the GitHub API in $((rate_limit_diff / 60)) minutes"
        remaining_time=$((3600 * (hours_passed + 1) - rate_limit_diff))
        echo "Sleeping for $remaining_time seconds..."
        sleep $remaining_time
        echo "Resuming..."
        BKG_RATE_LIMIT_START=$(date +%s)
        BKG_CALLS_TO_API=0
    fi

    # wait if 900 or more calls have been made in the last minute
    rate_limit_end=$(date +%s)
    sec_limit_diff=$((rate_limit_end - minute_start))
    min_passed=$((sec_limit_diff / 60))

    if ((minute_calls >= 900 * (min_passed + 1))); then
        echo "$minute_calls calls to the GitHub API in $sec_limit_diff seconds"
        remaining_time=$((60 * (min_passed + 1) - sec_limit_diff))
        echo "Sleeping for $remaining_time seconds..."
        sleep $remaining_time
        echo "Resuming..."
        minute_start=$(date +%s)
        minute_calls=0
    fi
}

[ -f "$BKG_INDEX_DB" ] || command curl -sSLNZO "https://github.com/$GITHUB_OWNER/$GITHUB_REPO/releases/latest/download/$BKG_INDEX_SQL.tar.zst" && tar -x -I 'zstd -d' -f "$BKG_INDEX_SQL.tar.zst" | sqlite3 "$BKG_INDEX_DB" || :
[ -f "$BKG_INDEX_DB" ] || touch "$BKG_INDEX_DB"
table_pkg="create table if not exists '$BKG_INDEX_TBL_PKG' (
    owner_id text,
    owner_type text not null,
    package_type text not null,
    owner text not null,
    repo text not null,
    package text not null,
    downloads integer not null,
    downloads_month integer not null,
    downloads_week integer not null,
    downloads_day integer not null,
    size integer not null,
    date text not null,
    primary key (owner_type, package_type, owner, repo, package, date)
); pragma auto_vacuum = full;"
sqlite3 "$BKG_INDEX_DB" "$table_pkg"

# copy and replace table to replace owner in primary key with owner_id
table_pkg_temp="create table if not exists '${BKG_INDEX_TBL_PKG}_temp' (
    owner_id text,
    owner_type text not null,
    package_type text not null,
    owner text not null,
    repo text not null,
    package text not null,
    downloads integer not null,
    downloads_month integer not null,
    downloads_week integer not null,
    downloads_day integer not null,
    size integer not null,
    date text not null,
    primary key (owner_type, package_type, owner_id, repo, package, date)
); pragma auto_vacuum = full;"
sqlite3 "$BKG_INDEX_DB" "$table_pkg_temp"
sqlite3 "$BKG_INDEX_DB" "insert or ignore into '${BKG_INDEX_TBL_PKG}_temp' select * from '$BKG_INDEX_TBL_PKG';"
sqlite3 "$BKG_INDEX_DB" "drop table '$BKG_INDEX_TBL_PKG';"
sqlite3 "$BKG_INDEX_DB" "alter table '${BKG_INDEX_TBL_PKG}_temp' rename to '$BKG_INDEX_TBL_PKG';"

trap '[ "$?" -eq "2" ] && exit 0 || xz_db' EXIT
