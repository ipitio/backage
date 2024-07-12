#!/bin/bash
# Setup the environment
# Usage: ./lib.sh
# Dependencies: curl
# Copyright (c) ipitio
#
# shellcheck disable=SC1090,SC1091,SC2015,SC2034

if ! command -v curl &>/dev/null || ! command -v jq &>/dev/null || ! command -v sqlite3 &>/dev/null || ! command -v zstd &>/dev/null || ! command -v parallel &>/dev/null; then
    echo "Installing dependencies..."
    sudo apt-get update
    sudo apt-get install curl jq parallel sqlite3 zstd -y
fi

# shellcheck disable=SC2046
. $(which env_parallel.bash)
env_parallel --session
[ ! -f .env ] || source .env
source env.env
SCRIPT_START=$(date -u +%s)
TODAY=$(date -u +%Y-%m-%d)
CORES=$(nproc)
printf -v MAX %x -1 && printf -v MAX %d 0x"${MAX/f/7}"

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

sqlite3() {
    command sqlite3 -init <(echo "
.output /dev/null
.timeout 100000
PRAGMA synchronous = OFF;
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = MEMORY;
PRAGMA locking_mode = EXCLUSIVE;
PRAGMA cache_size = -500000;
.output stdout
") "$@" 2>/dev/null
}

run_parallel() {
    # run the function in parallel
    (
        IFS=$'\n'
        for i in $2; do
            "$1" "$i" &
        done
        wait
    ) &
    all=$!

    # wait for the function to finish
    wait "$all"
}

get_BKG() {
    grep -E "^$1=" env.env | cut -d '=' -f2
}

set_BKG() {
    sed -i "s/^$1=.*/$1=$2/" env.env
}

if [ ! -f "$(get_BKG BKG_INDEX_DB)" ]; then
    command curl -sSLNZO "https://github.com/$GITHUB_OWNER/$GITHUB_REPO/releases/latest/download/$(get_BKG BKG_INDEX_SQL).zst"
    zstd -d "$(get_BKG BKG_INDEX_SQL).zst" | sqlite3 "$(get_BKG BKG_INDEX_DB)"
fi

[ -f "$(get_BKG BKG_INDEX_DB)" ] || sqlite3 "$(get_BKG BKG_INDEX_DB)" ""
table_pkg="create table if not exists '$(get_BKG BKG_INDEX_TBL_PKG)' (
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
sqlite3 "$(get_BKG BKG_INDEX_DB)" "$table_pkg"
