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
SCRIPT_START=$(date -u +%s)
TODAY=$(date -u +%Y-%m-%d)
readonly SCRIPT_START TODAY
printf -v MAX %x -1 && printf -v MAX %d 0x"${MAX/f/7}"

if ! command -v curl &>/dev/null || ! command -v jq &>/dev/null || ! command -v sqlite3 &>/dev/null || ! command -v zstd &>/dev/null; then
    echo "Installing dependencies..."
    sudo apt-get update
    sudo apt-get install curl jq sqlite3 zstd -y
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

[ -f "$BKG_INDEX_DB" ] || { command curl -sSLNZO "https://github.com/$GITHUB_OWNER/$GITHUB_REPO/releases/latest/download/$BKG_INDEX_SQL.zst" && zstd -d "$BKG_INDEX_SQL.zst" | sqlite3 "$BKG_INDEX_DB" || :; } || :
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
    primary key (owner_type, package_type, owner_id, repo, package, date)
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
