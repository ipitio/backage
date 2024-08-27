#!/bin/bash
# shellcheck disable=SC1091,SC2015

source lib/owner.sh

main() {
    local mode=0
    local rotated=false
    local owners
    local repos
    local packages
    local today
    local pkg_left
    local pkg_all

    while getopts "m:" flag; do
        case ${flag} in
        m)
            mode=${OPTARG}
            ;;
        ?)
            echo "Invalid option found: -${OPTARG}."
            exit 1
            ;;
        esac
    done

    set_BKG BKG_OWNERS_QUEUE ""
    set_BKG BKG_TIMEOUT "0"
    set_BKG BKG_SCRIPT_START "$(date -u +%s)"
    [ -n "$(get_BKG BKG_LAST_SCANNED_ID)" ] || set_BKG BKG_LAST_SCANNED_ID "0"
    today=$(date -u +%Y-%m-%d)
    BKG_BATCH_FIRST_STARTED=$(get_BKG BKG_BATCH_FIRST_STARTED)
    [ ! -f "$BKG_INDEX_SQL.zst" ] || unzstd -v -c "$BKG_INDEX_SQL.zst" | sqlite3 "$BKG_INDEX_DB"
    [ -f "$BKG_INDEX_DB" ] || sqlite3 "$BKG_INDEX_DB" ""
    sqlite3 "$BKG_INDEX_DB" "create table if not exists '$BKG_INDEX_TBL_PKG' (
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
        primary key (owner_id, package, date)
    ); pragma auto_vacuum = full;"
    sqlite3 "$BKG_INDEX_DB" "select owner_id, owner, repo, package from '$BKG_INDEX_TBL_PKG' where date >= '$BKG_BATCH_FIRST_STARTED';" | sort -u >packages_already_updated
    sqlite3 "$BKG_INDEX_DB" "select owner_id, owner, repo, package from '$BKG_INDEX_TBL_PKG';" | sort -u >packages_all
    comm -13 packages_already_updated packages_all >packages_to_update
    pkg_left=$(wc -l <packages_to_update)
    pkg_all=$(wc -l <packages_all)
    echo "all: $pkg_all"
    echo "done: $(wc -l <packages_already_updated)"
    echo "left: $pkg_left"
    [ -n "$(get_BKG BKG_BATCH_FIRST_STARTED)" ] || set_BKG BKG_BATCH_FIRST_STARTED "$today"

    # if this is a scheduled update, scrape all owners
    if [ "$mode" -eq 0 ]; then
        sed -i '/^\s*$/d' "$BKG_OWNERS"
        echo >>"$BKG_OWNERS"
        awk 'NF' "$BKG_OWNERS" >owners.tmp && mv owners.tmp "$BKG_OWNERS"
        sed -i 's/^[[:space:]]*//;s/[[:space:]]*$//' "$BKG_OWNERS"
        find "$BKG_INDEX_DIR" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort -u | awk '{print $1}' >>"$BKG_OWNERS"
        awk '!seen[$0]++' "$BKG_OWNERS" >owners.tmp && mv owners.tmp "$BKG_OWNERS"
        # remove lines from $BKG_OWNERS that are in $packages_all
        echo "$(
            awk -F'|' '{print $1"/"$2}' <packages_all
            awk -F'|' '{print $2}' <packages_all
        )" | sort -u | parallel "sed -i '\,^{}$,d' $BKG_OWNERS"

        if [[ "$pkg_left" == "0" || "$(get_BKG BKG_LEFT)" == "$pkg_left$pkg_all" ]]; then
            set_BKG BKG_BATCH_FIRST_STARTED "$today"
            pkg_left=$pkg_all
            rm -f packages_to_update
            \cp packages_all packages_to_update
            [ "$(wc -l <"$BKG_OWNERS")" -gt 10 ] || seq 1 10 | env_parallel --lb --halt soon,fail=1 page_owner
        fi

        sed -i '/^src$/d' "$BKG_OWNERS"
        sort -uR <"$BKG_OWNERS" | env_parallel --lb save_owner
        awk -F'|' '{print $1"/"$2}' <packages_to_update | sort -uR | env_parallel --lb save_owner
        set_BKG BKG_LEFT "$pkg_left$pkg_all"
    elif [ "$mode" -eq 1 ]; then
        save_owner arevindh
    fi

    BKG_BATCH_FIRST_STARTED=$(get_BKG BKG_BATCH_FIRST_STARTED)
    [ -n "$(get_BKG BKG_RATE_LIMIT_START)" ] || set_BKG BKG_RATE_LIMIT_START "$(date -u +%s)"
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

    [ -d "$BKG_INDEX_DIR" ] || mkdir "$BKG_INDEX_DIR"
    get_BKG_set BKG_OWNERS_QUEUE | env_parallel --lb update_owner
    echo "Compressing the database..."
    sqlite3 "$BKG_INDEX_DB" ".dump" | zstd -22 --ultra --long -T0 -o "$BKG_INDEX_SQL".new.zst

    if [ -f "$BKG_INDEX_SQL".new.zst ]; then
        # rotate the database if it's greater than 2GB
        if [ -f "$BKG_INDEX_SQL".zst ] && [ "$(stat -c %s "$BKG_INDEX_SQL".new.zst)" -ge 2000000000 ]; then
            rotated=true
            echo "Rotating the database..."
            local older_db
            older_db="$(date -u +%Y.%m.%d)".zst
            [ ! -f "$older_db" ] || rm -f "$older_db"
            mv "$BKG_INDEX_SQL".zst "$older_db"
            sqlite3 "$BKG_INDEX_DB" "delete from '$BKG_INDEX_TBL_PKG' where date < '$BKG_BATCH_FIRST_STARTED';"
            sqlite3 "$BKG_INDEX_DB" "select name from sqlite_master where type='table' and name like '${BKG_INDEX_TBL_VER}_%';" | parallel --lb "sqlite3 '$BKG_INDEX_DB' 'delete from {} where date < \"$BKG_BATCH_FIRST_STARTED\";'"
            sqlite3 "$BKG_INDEX_DB" "vacuum;"
            rm -f "$BKG_INDEX_SQL".new.zst
            sqlite3 "$BKG_INDEX_DB" ".dump" | zstd -22 --ultra --long -T0 -o "$BKG_INDEX_SQL".new.zst
            echo "Rotated the database"
        fi

        mv "$BKG_INDEX_SQL".new.zst "$BKG_INDEX_SQL".zst
        echo "Compressed the database"
    else
        echo "Failed to compress the database!"
    fi

    echo "Hydrating templates and cleaning up..."
    [ ! -f "$BKG_ROOT"/CHANGELOG.md ] || rm -f "$BKG_ROOT"/CHANGELOG.md
    \cp templates/.CHANGELOG.md "$BKG_ROOT"/CHANGELOG.md
    owners=$(sqlite3 "$BKG_INDEX_DB" "select count(distinct owner_id) from '$BKG_INDEX_TBL_PKG';")
    repos=$(sqlite3 "$BKG_INDEX_DB" "select count(distinct repo) from '$BKG_INDEX_TBL_PKG';")
    packages=$(sqlite3 "$BKG_INDEX_DB" "select count(distinct package) from '$BKG_INDEX_TBL_PKG';")
    perl -0777 -pe 's/\[OWNERS\]/'"$owners"'/g; s/\[REPOS\]/'"$repos"'/g; s/\[PACKAGES\]/'"$packages"'/g' "$BKG_ROOT"/CHANGELOG.md >CHANGELOG.tmp && [ -f CHANGELOG.tmp ] && mv CHANGELOG.tmp "$BKG_ROOT"/CHANGELOG.md || :
    ! $rotated || echo " The database grew over 2GB and was rotated, but you can find all previous data under [Releases](https://github.com/$GITHUB_OWNER/$GITHUB_REPO/releases)." >>"$BKG_ROOT"/CHANGELOG.md
    [ ! -f "$BKG_ROOT"/README.md ] || rm -f "$BKG_ROOT"/README.md
    \cp templates/.README.md "$BKG_ROOT"/README.md
    perl -0777 -pe 's/<GITHUB_OWNER>/'"$GITHUB_OWNER"'/g; s/<GITHUB_REPO>/'"$GITHUB_REPO"'/g; s/<GITHUB_BRANCH>/'"$GITHUB_BRANCH"'/g' "$BKG_ROOT"/README.md >README.tmp && [ -f README.tmp ] && mv README.tmp "$BKG_ROOT"/README.md || :
    sed -i '/^BKG_VERSIONS_.*=/d; /^BKG_PACKAGES_.*=/d; /^BKG_OWNERS_.*=/d; /^BKG_TIMEOUT=/d; /^BKG_SCRIPT_START=/d' "$BKG_ENV"
    \cp "$BKG_ROOT"/README.md "$BKG_ROOT"/index/README.md
    [ -d "$BKG_ROOT"/index/src ] || mkdir -p "$BKG_ROOT"/index/src
    [ -d "$BKG_ROOT"/index/src/img ] || mkdir -p "$BKG_ROOT"/index/src/img
    \cp img/logo-b.png "$BKG_ROOT"/index/src/img/logo-b.png
    \cp img/logo.ico "$BKG_ROOT"/index/favicon.ico
    \cp index.html "$BKG_ROOT"/index/index.html
    rm -f packages_already_updated packages_all packages_to_update
    echo "Done!"
}
