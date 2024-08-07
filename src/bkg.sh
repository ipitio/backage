#!/bin/bash
# shellcheck disable=SC1091,SC2015

source lib/owner.sh

main() {
    local mode=0
    local update=true

    while getopts "m:r" flag; do
        case ${flag} in
        m)
            mode=${OPTARG}
            ;;
        r)
            update=false
            ;;
        ?)
            echo "Invalid option found: -${OPTARG}."
            exit 1
            ;;
        esac
    done

    set_BKG BKG_TIMEOUT "0"
    set_BKG BKG_TODAY "$(date -u +%Y-%m-%d)"
    set_BKG BKG_SCRIPT_START "$(date -u +%s)"
    set_BKG BKG_AUTO "$mode"
    export BKG_TODAY
    BKG_TODAY=$(get_BKG BKG_TODAY)
    BKG_BATCH_FIRST_STARTED=$(get_BKG BKG_BATCH_FIRST_STARTED)
    [ ! -f "$BKG_INDEX_SQL.zst" ] || unzstd -v -c "$BKG_INDEX_SQL.zst" | sqlite3 "$BKG_INDEX_DB"
    [ -f "$BKG_INDEX_DB" ] || sqlite3 "$BKG_INDEX_DB" ""
    local table_pkg="create table if not exists '$BKG_INDEX_TBL_PKG' (
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

    if $update; then
        update_owners
    else
        sqlite3 "$BKG_INDEX_DB" "select distinct owner from '$BKG_INDEX_TBL_PKG' where date >= '$BKG_BATCH_FIRST_STARTED';" | env_parallel --lb refresh_owner
    fi

    del_BKG "BKG_VERSIONS_.*"
    del_BKG "BKG_PACKAGES_.*"
    del_BKG "BKG_OWNERS_.*"
    del_BKG BKG_TIMEOUT
    del_BKG BKG_TODAY
    del_BKG BKG_SCRIPT_START
    del_BKG BKG_AUTO
    sed -i '/^\s*$/d' env.env
    echo >>env.env
}
