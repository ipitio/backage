#!/bin/bash
# Backage library
# Usage: ./lib.sh
# Dependencies: curl, jq, sqlite3, zstd
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
[ ! -f .env ] || source .env 2>/dev/null
[ ! -f env.env ] || source env.env 2>/dev/null

# format numbers like 1000 to 1k
numfmt() {
    awk '{ split("k M B T P E Z Y", v); s=0; while( $1>999.9 ) { $1/=1000; s++ } print int($1*10)/10 v[s] }'
}

# format bytes to KB, MB, GB, etc.
numfmt_size() {
    awk '{ split("kB MB GB TB PB EB ZB YB", v); s=0; while( $1>999.9 ) { $1/=1000; s++ } print int($1*10)/10 " " v[s] }' | sed 's/ //'
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

get_BKG() {
    local file=env.env
    local key=$1

    while ! ln "$file" "$file.lock" 2>/dev/null; do
        sleep 0.1
    done

    res=""
    ! grep -q "^$key=" "$file" || res=$(grep "^$key=" "$file" | cut -d'=' -f2)
    rm -f "$file.lock"
    echo "$res"
}

set_BKG() {
    local file=env.env
    local key=$1
    local value
    local tmp_file
    value=$(echo "$2" | perl -pe 'chomp if eof')
    tmp_file=$(mktemp)

    while ! ln "$file" "$file.lock" 2>/dev/null; do
        sleep 0.1
    done

    if ! grep -q "^$key=" "$file"; then
        echo "$key=$value" >>"$file"
    else
        grep -v "^$key=" "$file" >"$tmp_file"
        echo "$key=$value" >>"$tmp_file"
        mv "$tmp_file" "$file"
    fi

    sed -i '/^\s*$/d' $file
    echo >>$file

    rm -f "$file.lock"
}

get_BKG_set() {
    get_BKG "$1" | perl -pe 's/\\n/\n/g'
}

set_BKG_set() {
    local list
    list=$(get_BKG_set "$1" | awk '!seen[$0]++' | perl -pe 's/\n/\\n/g')
    # shellcheck disable=SC2076
    [[ "$list" =~ "$2" ]] || list="${list:+$list\n}$2"
    set_BKG "$1" "$(echo "$list" | perl -pe 's/\\n/\n/g' | perl -pe 's/\n/\\n/g')"
}

del_BKG_set() {
    local list
    list=$(get_BKG_set "$1" | grep -v "$2" | perl -pe 's/\n/\\n/g')
    set_BKG "$1" "$list"
}

del_BKG() {
    local file=env.env
    local key=$1
    local tmp_file
    tmp_file=$(mktemp)

    while ! ln "$file" "$file.lock" 2>/dev/null; do
        sleep 0.1
    done

    grep -v "^$key=" "$file" >"$tmp_file"
    mv "$tmp_file" "$file"
    rm -f "$file.lock"
}

check_limit() {
    local total_calls
    local rate_limit_end
    local script_limit_diff
    local timeout
    local rate_limit_diff
    local hours_passed
    local remaining_time
    local minute_calls
    local sec_limit_diff
    local min_passed

    total_calls=$(get_BKG BKG_CALLS_TO_API)
    rate_limit_end=$(date -u +%s)
    script_limit_diff=$((rate_limit_end - $(get_BKG BKG_SCRIPT_START)))
    timeout=$(get_BKG BKG_TIMEOUT)

    # exit if the script has been running for 5 hours
    if ((script_limit_diff >= 18000)); then
        if ((timeout == 0)) && (($(get_BKG BKG_TIDY) == 1)); then
            echo "Script has been running for 5 hours! Tidying up..."
            set_BKG BKG_TIDY "0"
            set_BKG BKG_TIMEOUT "1"
        elif ((timeout == 2)); then
            return 1
        fi

        exit 0
    fi

    # wait if 1000 or more calls have been made in the last hour
    rate_limit_diff=$((rate_limit_end - $(get_BKG BKG_RATE_LIMIT_START)))
    hours_passed=$((rate_limit_diff / 3600))

    if ((total_calls >= 1000 * (hours_passed + 1))); then
        echo "$total_calls calls to the GitHub API in $((rate_limit_diff / 60)) minutes"
        remaining_time=$((3600 * (hours_passed + 1) - rate_limit_diff))
        echo "Sleeping for $remaining_time seconds..."
        sleep $remaining_time
        echo "Resuming..."
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
        echo "Sleeping for $remaining_time seconds..."
        sleep $remaining_time
        echo "Resuming..."
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
        check_limit
        sleep "$wait_time"
        ((i++))
        ((wait_time *= 2))
    done

    return 1
}

run_parallel() {
    (
        IFS=$'\n'
        for i in $2; do
            check_limit || exit
            "$1" "$i" &
        done
        wait
    ) &
    wait "$!"
}

clean_up() {
    [ -f "$BKG_INDEX_DB" ] || return 1
    local rotated=false
    local query
    local tables
    local owners
    local repos
    local packages
    local TODAY

    echo "Compressing the database..."
    TODAY=$(get_BKG BKG_TODAY)
    sqlite3 "$BKG_INDEX_DB" ".dump" | zstd -22 --ultra --long --no-progress -T0 -o "$BKG_INDEX_SQL".new.zst

    if [ -f "$BKG_INDEX_SQL".new.zst ]; then
        # rotate the database if it's greater than 2GB
        if [ "$(stat -c %s "$BKG_INDEX_SQL".new.zst)" -ge 2000000000 ]; then
            rotated=true
            echo "Rotating the database..."
            [ -d "$BKG_INDEX_SQL".d ] || mkdir "$BKG_INDEX_SQL".d
            [ ! -f "$BKG_INDEX_SQL".zst ] || mv "$BKG_INDEX_SQL".zst "$BKG_INDEX_SQL".d/"$(date -u +%Y.%m.%d)".zst
            sqlite3 "$BKG_INDEX_DB" "delete from '$BKG_INDEX_TBL_PKG' where date not between date('$BKG_BATCH_FIRST_STARTED') and date('$TODAY');"
            tables=$(sqlite3 "$BKG_INDEX_DB" "select name from sqlite_master where type='table' and name like '${BKG_INDEX_TBL_VER}_%';")

            for table in $tables; do
                sqlite3 "$BKG_INDEX_DB" "delete from '$table' where date not between date('$BKG_BATCH_FIRST_STARTED') and date('$TODAY');"
            done

            sqlite3 "$BKG_INDEX_DB" "vacuum;"
            sqlite3 "$BKG_INDEX_DB" ".dump" | zstd -22 --ultra --long -T0 --no-progress -o "$BKG_INDEX_SQL".new.zst
        fi

        mv "$BKG_INDEX_SQL".new.zst "$BKG_INDEX_SQL".zst
        echo "Compressed the database"
    else
        echo "Failed to compress the database!"
    fi

    echo "Updating templates..."
    [ ! -f ../CHANGELOG.md ] || rm -f ../CHANGELOG.md
    \cp templates/.CHANGELOG.md ../CHANGELOG.md
    owners=$(sqlite3 "$BKG_INDEX_DB" "select count(distinct owner_id) from '$BKG_INDEX_TBL_PKG';")
    repos=$(sqlite3 "$BKG_INDEX_DB" "select count(distinct repo) from '$BKG_INDEX_TBL_PKG';")
    packages=$(sqlite3 "$BKG_INDEX_DB" "select count(distinct package) from '$BKG_INDEX_TBL_PKG';")
    perl -0777 -pe 's/\[OWNERS\]/'"$owners"'/g; s/\[REPOS\]/'"$repos"'/g; s/\[PACKAGES\]/'"$packages"'/g' ../CHANGELOG.md >CHANGELOG.tmp && [ -f CHANGELOG.tmp ] && mv CHANGELOG.tmp ../CHANGELOG.md || :
    ! $rotated || echo " The database grew over 2GB and was rotated, but you can find all previous data under [Releases](https://github.com/$GITHUB_OWNER/$GITHUB_REPO/releases)." >>../CHANGELOG.md
    [ ! -f ../README.md ] || rm -f ../README.md
    \cp templates/.README.md ../README.md
    perl -0777 -pe 's/<GITHUB_OWNER>/'"$GITHUB_OWNER"'/g; s/<GITHUB_REPO>/'"$GITHUB_REPO"'/g; s/<GITHUB_BRANCH>/'"$GITHUB_BRANCH"'/g' ../README.md >README.tmp && [ -f README.tmp ] && mv README.tmp ../README.md || :
    echo "Updated templates"

    # if index db is greater than 100MB, remove it
    if [ "$(stat -c %s "$BKG_INDEX_DB")" -ge 100000000 ]; then
        echo "Removing the database..."

        if [ -f "$BKG_INDEX_DB" ]; then
            git rm "$BKG_INDEX_DB" || rm -f "$BKG_INDEX_DB"
        fi

        if [ -f ../index.json ]; then
            git rm ../index.json || rm -f ../index.json
        fi

        echo "Removed the database"
    fi

    # these should have been deleted but jic
    del_BKG "BKG_VERSIONS_.*"
}

_jq() {
    echo "$1" | base64 --decode | jq -r "${@:2}"
}

save_version() {
    check_limit || return
    [ -n "$1" ] || return
    local id
    local name
    local tags
    local versions_json
    id=$(_jq "$1" '.id')
    echo "Queuing $owner/$package/$id..."
    name=$(_jq "$1" '.name')
    tags=$(_jq "$1" '.. | try .tags | join(",")')
    versions_json=$(get_BKG BKG_VERSIONS_JSON_"${owner}_${package}")
    [ -n "$versions_json" ] && jq -e . <<<"$versions_json" &>/dev/null && : || versions_json="[]"
    jq -e ".[] | select(.id == \"$id\")" <<<"$versions_json" &>/dev/null || versions_json=$(jq -c ". + [{\"id\":\"$id\",\"name\":\"$name\",\"tags\":\"$tags\"}]" <<<"$versions_json")
    set_BKG BKG_VERSIONS_JSON_"${owner}_${package}" "$versions_json"
    echo "Queued $owner/$package/$id"
}

page_version() {
    check_limit || return
    [ -n "$1" ] || return
    local versions_json_more="[]"
    local calls_to_api
    local min_calls_to_api
    echo "Searching for more versions of $owner/$package on page $1..."

    if [ -n "$GITHUB_TOKEN" ]; then
        versions_json_more=$(curl -H "Accept: application/vnd.github+json" \
            -H "Authorization: Bearer $GITHUB_TOKEN" \
            -H "X-GitHub-Api-Version: 2022-11-28" \
            "https://api.github.com/$owner_type/$owner/packages/$package_type/$package/versions?per_page=$BKG_VERSIONS_PER_PAGE&page=$1")
        calls_to_api=$(get_BKG BKG_CALLS_TO_API)
        min_calls_to_api=$(get_BKG BKG_MIN_CALLS_TO_API)
        ((calls_to_api++))
        ((min_calls_to_api++))
        set_BKG BKG_CALLS_TO_API "$calls_to_api"
        set_BKG BKG_MIN_CALLS_TO_API "$min_calls_to_api"
        jq -e . <<<"$versions_json_more" &>/dev/null || versions_json_more="[]"
    fi

    # if versions doesn't have .name, break
    if ! jq -e '.[].name' <<<"$versions_json_more" &>/dev/null; then
        set_BKG BKG_VERSIONS_PAGE_"${owner}_${package}" "-$1"
        return 1
    fi

    # add the new versions to the versions_json, if they are not already there
    set_BKG BKG_VERSIONS_PAGE_"${owner}_${package}" "$1"
    run_parallel save_version "$(jq -r '.[] | @base64' <<<"$versions_json_more")"
    echo "Searched $owner/$package page $1"
}

update_version() {
    check_limit || return
    [ -n "$1" ] || return
    local version_size=-1
    local version_raw_downloads=-1
    local version_raw_downloads_month=-1
    local version_raw_downloads_week=-1
    local version_raw_downloads_day=-1
    local version_html
    local version_name
    local version_tags
    local version_size
    local version_id
    local version_newest_id
    local table_version_name
    local query
    local count
    local manifest
    local sep
    local TODAY
    version_id=$(_jq "$1" '.id')
    version_name=$(_jq "$1" '.name')
    version_tags=$(_jq "$1" '.tags')
    TODAY=$(get_BKG BKG_TODAY)
    echo "Updating $owner/$package/$version_id..."
    table_version_name="${BKG_INDEX_TBL_VER}_${owner_type}_${package_type}_${owner}_${repo}_${package}"
    table_version="create table if not exists '$table_version_name' (
        id text not null,
        name text not null,
        size integer not null,
        downloads integer not null,
        downloads_month integer not null,
        downloads_week integer not null,
        downloads_day integer not null,
        date text not null,
        tags text,
        primary key (id, date)
    );"
    sqlite3 "$BKG_INDEX_DB" "$table_version"
    search="select count(*) from '$table_version_name' where id='$version_id' and date between date('$BKG_BATCH_FIRST_STARTED') and date('$TODAY');"
    count=$(sqlite3 "$BKG_INDEX_DB" "$search")

    # insert a new row
    if [[ "$count" =~ ^0*$ || "$owner" == "arevindh" ]]; then
        if [ "$package_type" = "container" ]; then
            # get the size by adding up the layers
            [[ "$version_name" =~ ^sha256:.+$ ]] && sep="@" || sep=":"
            manifest=$(docker manifest inspect -v "ghcr.io/$lower_owner/$lower_package$sep$version_name" 2>&1)

            if [[ -n "$(jq '.. | try .layers[]' 2>/dev/null <<<"$manifest")" ]]; then
                version_size=$(jq '.. | try .size | select(. > 0)' <<<"$manifest" | awk '{s+=$1} END {print s}')
                [[ "$version_size" =~ ^[0-9]+$ ]] || version_size=-1
            elif [[ -n "$(jq '.. | try .manifests[]' 2>/dev/null <<<"$manifest")" ]]; then
                version_size=$(jq '.. | try .size | select(. > 0)' <<<"$manifest" | awk '{s+=$1} END {print s/NR}')
                [[ "$version_size" =~ ^[0-9]+$ ]] || version_size=-1
            fi
        else
            : # TODO: support other package types
        fi

        # get the downloads
        version_html=$(curl "https://github.com/$owner/$repo/pkgs/$package_type/$package/$version_id")
        version_raw_downloads=$(echo "$version_html" | grep -Pzo 'Total downloads<[^<]*<[^<]*' | grep -Pzo '\d*$' | tr -d '\0')
        version_raw_downloads=$(tr -d ',' <<<"$version_raw_downloads")

        if [[ "$version_raw_downloads" =~ ^[0-9]+$ ]]; then
            version_raw_downloads_month=$(grep -Pzo 'Last 30 days<[^<]*<[^<]*' <<<"$version_html" | grep -Pzo '\d*$' | tr -d '\0')
            version_raw_downloads_week=$(grep -Pzo 'Last week<[^<]*<[^<]*' <<<"$version_html" | grep -Pzo '\d*$' | tr -d '\0')
            version_raw_downloads_day=$(grep -Pzo 'Today<[^<]*<[^<]*' <<<"$version_html" | grep -Pzo '\d*$' | tr -d '\0')
            version_raw_downloads_month=$(tr -d ',' <<<"$version_raw_downloads_month")
            version_raw_downloads_week=$(tr -d ',' <<<"$version_raw_downloads_week")
            version_raw_downloads_day=$(tr -d ',' <<<"$version_raw_downloads_day")
        else
            version_raw_downloads=-1
            version_raw_downloads_month=-1
            version_raw_downloads_week=-1
            version_raw_downloads_day=-1
        fi

        sqlite3 "$BKG_INDEX_DB" "insert or replace into '$table_version_name' (id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('$version_id', '$version_name', '$version_size', '$version_raw_downloads', '$version_raw_downloads_month', '$version_raw_downloads_week', '$version_raw_downloads_day', '$TODAY', '$version_tags');"
    fi
    echo "Updated $owner/$package/$version_id"
}

save_package() {
    check_limit || return
    local package_new
    local package_type
    local repo
    local packages
    package_new=$(cut -d'/' -f7 <<<"$1" | tr -d '"')
    package_new=${package_new%/}
    [ -n "$package_new" ] || return
    echo "Queuing $owner/$package_new..."
    package_type=$(cut -d'/' -f5 <<<"$1")
    repo=$(grep -zoP '(?<=href="/'"$owner_type"'/'"$owner"'/packages/'"$package_type"'/package/'"$package_new"'")(.|\n)*?href="/'"$owner"'/[^"]+"' <<<"$html" | tr -d '\0' | grep -oP 'href="/'"$owner"'/[^"]+' | cut -d'/' -f3)
    package_type=${package_type%/}
    repo=${repo%/}
    set_BKG_set BKG_PACKAGES_"$owner" "$package_type/$repo/$package_new"
    echo "Queued $owner/$package_new"
}

page_package() {
    check_limit || return
    local packages_lines
    local html
    [ "$owner_type" = "orgs" ] && html=$(curl "https://github.com/$owner_type/$owner/packages?visibility=public&per_page=100&page=$1") || html=$(curl "https://github.com/$owner?tab=packages&visibility=public&&per_page=100&page=$1")
    packages_lines=$(grep -zoP 'href="/'"$owner_type"'/'"$owner"'/packages/[^/]+/package/[^"]+"' <<<"$html" | tr -d '\0')
    [ -n "$packages_lines" ] || return 1
    echo "Searching for packages by $owner on page $1..."
    packages_lines=${packages_lines//href=/\\nhref=}
    packages_lines=${packages_lines//\\n/$'\n'} # replace \n with newline
    run_parallel save_package "$packages_lines"
    echo "Searched $owner page $1"
}

update_package() {
    check_limit || return
    [ -n "$1" ] || return
    local html
    local query
    local count
    package_type=$(cut -d'/' -f1 <<<"$1")
    repo=$(cut -d'/' -f2 <<<"$1")
    package=$(cut -d'/' -f3 <<<"$1")
    package=${package%/}
    TODAY=$(get_BKG BKG_TODAY)

    if grep -q "$owner/$repo/$package" "$BKG_OPTOUT"; then
        sqlite3 "$BKG_INDEX_DB" "delete from '$BKG_INDEX_TBL_PKG' where owner_id='$owner_id' and package='$package';"
        sqlite3 "$BKG_INDEX_DB" "drop table if exists '${BKG_INDEX_TBL_VER}_${owner_type}_${package_type}_${owner}_${repo}_${package}';"
        return
    fi

    # manual update: skip if the package is already in the index; the rest are updated daily
    if [ "$1" = "1" ] && [[ "$owner" != "arevindh" ]]; then
        [[ "$(sqlite3 "$BKG_INDEX_DB" "select count(*) from '$BKG_INDEX_TBL_PKG' where owner_id='$owner_id' and package='$package';")" =~ ^0*$ ]] || return
    fi

    # update stats
    if [[ "$(sqlite3 "$BKG_INDEX_DB" "select count(*) from '$BKG_INDEX_TBL_PKG' where owner_id='$owner_id' and package='$package' and date between date('$BKG_BATCH_FIRST_STARTED') and date('$TODAY');")" =~ ^0*$ || "$owner" == "arevindh" ]]; then
        raw_downloads=-1
        raw_downloads_month=-1
        raw_downloads_week=-1
        raw_downloads_day=-1
        size=-1

        # decode percent-encoded characters and make lowercase (eg. for docker manifest)
        if [ "$package_type" = "container" ]; then
            lower_owner=$owner
            lower_package=$package

            for i in "$lower_owner" "$lower_package"; do
                i=${i//%/%25}
            done

            lower_owner=$(perl -pe 's/%([0-9A-Fa-f]{2})/chr(hex($1))/eg' <<<"$lower_owner" | tr '[:upper:]' '[:lower:]')
            lower_package=$(perl -pe 's/%([0-9A-Fa-f]{2})/chr(hex($1))/eg' <<<"$lower_package" | tr '[:upper:]' '[:lower:]')
        fi

        # scrape the package page for the total downloads
        html=$(curl "https://github.com/$owner/$repo/pkgs/$package_type/$package")
        [ -n "$(grep -Pzo 'Total downloads' <<<"$html" | tr -d '\0')" ] || return
        raw_downloads=$(grep -Pzo 'Total downloads[^"]*"\d*' <<<"$html" | grep -Pzo '\d*$' | tr -d '\0') # https://stackoverflow.com/a/74214537
        [[ "$raw_downloads" =~ ^[0-9]+$ ]] || raw_downloads=-1
        set_BKG BKG_VERSIONS_PAGE_"${owner}_${package}" "0"

        # add all the versions currently in the db to the versions_json, if they are not already there
        table_version_name="${BKG_INDEX_TBL_VER}_${owner_type}_${package_type}_${owner}_${repo}_${package}"
        [ -z "$(sqlite3 "$BKG_INDEX_DB" "select name from sqlite_master where type='table' and name='$table_version_name';")" ] || run_parallel save_version "$(jq -r '.[] | @base64' <<<"$(sqlite3 -json "$BKG_INDEX_DB" "select id, name, tags from '$table_version_name';")")"
        echo "Getting versions for $owner/$package..."

        while check_limit; do
            versions_page=$(get_BKG BKG_VERSIONS_PAGE_"${owner}_${package}")
            [[ "$versions_page" =~ ^\d+$ ]] || break
            version_pages=$(seq "$((versions_page + 1))" "${versions_page}0")

            if [ -n "$version_pages" ]; then
                version_pages=$(seq "$((versions_page + 1))" "$(get_BKG BKG_MAX)")
                [ -n "$version_pages" ] || break
            fi

            run_parallel page_version "$version_pages"
        done

        echo "Got versions for $owner/$package"
        versions_json=$(get_BKG BKG_VERSIONS_JSON_"${owner}_${package}")
        jq -e . <<<"$versions_json" &>/dev/null || versions_json="[{\"id\":\"latest\",\"name\":\"latest\"}]"
        echo "Scraping $owner/$package..."
        run_parallel update_version "$(jq -r '.[] | @base64' <<<"$versions_json")"
        del_BKG BKG_VERSIONS_JSON_"${owner}_${package}"
        del_BKG BKG_VERSIONS_PAGE_"${owner}_${package}"
        check_limit

        if [ -n "$(sqlite3 "$BKG_INDEX_DB" "select name from sqlite_master where type='table' and name='$table_version_name';")" ]; then
            # calculate the total downloads
            max_date=$(sqlite3 "$BKG_INDEX_DB" "select max(date) from '$table_version_name';")
            query="select sum(downloads), sum(downloads_month), sum(downloads_week), sum(downloads_day) from '$table_version_name' where date='$max_date';"
            # summed_raw_downloads=$(sqlite3 "$BKG_INDEX_DB" "$query" | cut -d'|' -f1)
            raw_downloads_month=$(sqlite3 "$BKG_INDEX_DB" "$query" | cut -d'|' -f2)
            raw_downloads_week=$(sqlite3 "$BKG_INDEX_DB" "$query" | cut -d'|' -f3)
            raw_downloads_day=$(sqlite3 "$BKG_INDEX_DB" "$query" | cut -d'|' -f4)

            # use the latest version's size as the package size
            version_newest_id=$(sqlite3 "$BKG_INDEX_DB" "select id from '${BKG_INDEX_TBL_VER}_${owner_type}_${package_type}_${owner}_${repo}_${package}' order by id desc limit 1;")
            size=$(sqlite3 "$BKG_INDEX_DB" "select size from '${BKG_INDEX_TBL_VER}_${owner_type}_${package_type}_${owner}_${repo}_${package}' where id='$version_newest_id' order by date desc limit 1;")
        fi

        sqlite3 "$BKG_INDEX_DB" "insert or replace into '$BKG_INDEX_TBL_PKG' (owner_id, owner_type, package_type, owner, repo, package, downloads, downloads_month, downloads_week, downloads_day, size, date) values ('$owner_id', '$owner_type', '$package_type', '$owner', '$repo', '$package', '$raw_downloads', '$raw_downloads_month', '$raw_downloads_week', '$raw_downloads_day', '$size', '$TODAY');"
        echo "Scraped $owner/$package"
    fi
}

check_owner() {
    check_limit || return
    [ -n "$1" ] || return
    local owner
    local id
    owner=$(_jq "$1" '.login')
    id=$(_jq "$1" '.id')
    echo "Checking $owner..."
    set_BKG_set BKG_OWNERS_QUEUE "$id/$owner"
    [ "$(stat -c %s "$BKG_OWNERS")" -ge 100000000 ] && del_BKG_set BKG_OWNERS_QUEUE "$id/$owner" || set_BKG BKG_LAST_SCANNED_ID "$id"
    echo "Checked $owner"
}

page_owner() {
    check_limit || return
    echo "Searching for more packages on API page $1..."
    local owners_more="[]"
    local calls_to_api
    local min_calls_to_api

    if [ -n "$GITHUB_TOKEN" ]; then
        owners_more=$(curl -H "Accept: application/vnd.github+json" \
            -H "Authorization: Bearer $GITHUB_TOKEN" \
            -H "X-GitHub-Api-Version: 2022-11-28" \
            "https://api.github.com/users?per_page=100&page=$1&since=$(get_BKG BKG_LAST_SCANNED_ID)")
        calls_to_api=$(get_BKG BKG_CALLS_TO_API)
        min_calls_to_api=$(get_BKG BKG_MIN_CALLS_TO_API)
        ((calls_to_api++))
        ((min_calls_to_api++))
        set_BKG BKG_CALLS_TO_API "$calls_to_api"
        set_BKG BKG_MIN_CALLS_TO_API "$min_calls_to_api"
        jq -e . <<<"$owners_more" &>/dev/null || owners_more="[]"
    fi

    # if owners doesn't have .login, break
    jq -e '.[].login' <<<"$owners_more" &>/dev/null || return 1
    run_parallel check_owner "$(jq -r '.[] | @base64' <<<"$owners_more")"
    echo "Searched API page $1"
}

remove_owner() {
    check_limit || return
    [ -n "$1" ] || return

    if [[ "$(sqlite3 "$BKG_INDEX_DB" "select count(*) from '$BKG_INDEX_TBL_PKG' where owner_id='$1' and date between date('$BKG_BATCH_FIRST_STARTED') and date('$TODAY');")" =~ ^0*$ ]]; then
        echo "Removing $1..."
        del_BKG_set BKG_OWNERS_QUEUE "$1"
        echo "Removed $1"
    fi
}

add_owner() {
    check_limit || return
    owner=$(echo "$1" | tr -d '[:space:]')
    [ -n "$owner" ] || return
    echo "Adding $owner..."
    owner_id=""
    local calls_to_api
    local min_calls_to_api

    if [[ "$owner" =~ .*\/.* ]]; then
        owner_id=$(cut -d'/' -f1 <<<"$owner")
        owner=$(cut -d'/' -f2 <<<"$owner")
    fi

    if [ -z "$owner_id" ]; then
        owner_id=$(curl -H "Accept: application/vnd.github+json" \
            -H "Authorization: Bearer $GITHUB_TOKEN" \
            -H "X-GitHub-Api-Version: 2022-11-28" \
            "https://api.github.com/users/$owner" | jq -r '.id')
        calls_to_api=$(get_BKG BKG_CALLS_TO_API)
        min_calls_to_api=$(get_BKG BKG_MIN_CALLS_TO_API)
        ((calls_to_api++))
        ((min_calls_to_api++))
        set_BKG BKG_CALLS_TO_API "$calls_to_api"
        set_BKG BKG_MIN_CALLS_TO_API "$min_calls_to_api"
    fi

    set_BKG_set BKG_OWNERS_QUEUE "$owner_id/$owner"
    echo "Added $owner"
}

update_owner() {
    set_BKG BKG_TIMEOUT "0"
    check_limit || return
    [ -n "$1" ] || return
    local login_id=$1
    local html
    [ -n "$login_id" ] || return
    owner=$(cut -d'/' -f2 <<<"$login_id")
    echo "Updating $owner..."
    owner_id=$(cut -d'/' -f1 <<<"$login_id")
    owner_type="orgs"
    html=$(curl "https://github.com/orgs/$owner/people")
    is_org=$(grep -zoP 'href="/orgs/'"$owner"'/people"' <<<"$html" | tr -d '\0')
    [ -n "$is_org" ] || owner_type="users"
    set_BKG BKG_PACKAGES_"$owner" ""
    run_parallel page_package "$(seq 1 100)"
    run_parallel update_package "$(get_BKG_set BKG_PACKAGES_"$owner")"
    del_BKG BKG_PACKAGES_"$owner"
    echo "Updated $owner"
}

refresh_package() {
    check_limit || return
    [ -n "$1" ] || return
    local script_diff
    local fmt_downloads
    local version_count
    local version_with_tag_count
    local table_version_name
    local query
    local version_newest_id
    IFS='|' read -r owner_id owner_type package_type owner repo package downloads downloads_month downloads_week downloads_day size date <<<"$1"
    echo "Refreshing $owner/$package..."
    [ -d "$BKG_INDEX_DIR/$owner/$repo" ] || mkdir "$BKG_INDEX_DIR/$owner/$repo"
    script_diff=$(($(date -u +%s) - $(get_BKG BKG_SCRIPT_START)))

    if ((script_diff >= 21500)); then
        echo "Script has been running for 6 hours. Committing changes..."
        exit
    fi

    # only use the latest date for the package
    max_date=$(sqlite3 "$BKG_INDEX_DB" "select date from '$BKG_INDEX_TBL_PKG' where owner_id='$owner_id' and package='$package' order by date desc limit 1;")
    [ "$date" = "$max_date" ] || return
    fmt_downloads=$(numfmt <<<"$downloads")
    version_count=0
    version_with_tag_count=0
    table_version_name="${BKG_INDEX_TBL_VER}_${owner_type}_${package_type}_${owner}_${repo}_${package}"

    if [ -n "$(sqlite3 "$BKG_INDEX_DB" "select name from sqlite_master where type='table' and name='$table_version_name';")" ]; then
        version_count=$(sqlite3 "$BKG_INDEX_DB" "select count(distinct id) from '$table_version_name';")
        version_with_tag_count=$(sqlite3 "$BKG_INDEX_DB" "select count(distinct id) from '$table_version_name' where tags != '' and tags is not null;")
    fi

    echo "{" >>"$BKG_INDEX_DIR/$owner/$repo/$package".json
    [[ "$package_type" != "container" ]] || echo "\"image\": \"$package\",\"pulls\": \"$fmt_downloads\"," >>"$BKG_INDEX_DIR/$owner/$repo/$package".json
    echo "\"owner_type\": \"$owner_type\",
        \"package_type\": \"$package_type\",
        \"owner_id\": \"$owner_id\",
        \"owner\": \"$owner\",
        \"repo\": \"$repo\",
        \"package\": \"$package\",
        \"date\": \"$date\",
        \"size\": \"$(numfmt_size <<<"$size")\",
        \"versions\": \"$(numfmt <<<"$version_count")\",
        \"tagged\": \"$(numfmt <<<"$version_with_tag_count")\",
        \"downloads\": \"$fmt_downloads\",
        \"downloads_month\": \"$(numfmt <<<"$downloads_month")\",
        \"downloads_week\": \"$(numfmt <<<"$downloads_week")\",
        \"downloads_day\": \"$(numfmt <<<"$downloads_day")\",
        \"raw_size\": ${size:--1},
        \"raw_versions\": ${version_count:--1},
        \"raw_tagged\": ${version_with_tag_count:--1},
        \"raw_downloads\": ${downloads:--1},
        \"raw_downloads_month\": ${downloads_month:--1},
        \"raw_downloads_week\": ${downloads_week:--1},
        \"raw_downloads_day\": ${downloads_day:--1},
        \"version\": [" >>"$BKG_INDEX_DIR/$owner/$repo/$package".json

    # add the versions to index/"$owner".json
    if [ "$version_count" -gt 0 ]; then
        version_newest_id=$(sqlite3 "$BKG_INDEX_DB" "select id from '$table_version_name' order by id desc limit 1;")
        query="select id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags from '$table_version_name' group by id order by id desc;"
        sqlite3 "$BKG_INDEX_DB" "$query" | while IFS='|' read -r vid vname vsize vdownloads vdownloads_month vdownloads_week vdownloads_day vdate vtags; do
            echo "{
                \"id\": ${vid:--1},
                \"name\": \"$vname\",
                \"date\": \"$vdate\",
                \"newest\": $([ "$vid" = "$version_newest_id" ] && echo "true" || echo "false"),
                \"size\": \"$(numfmt_size <<<"$vsize")\",
                \"downloads\": \"$(numfmt <<<"$vdownloads")\",
                \"downloads_month\": \"$(numfmt <<<"$vdownloads_month")\",
                \"downloads_week\": \"$(numfmt <<<"$vdownloads_week")\",
                \"downloads_day\": \"$(numfmt <<<"$vdownloads_day")\",
                \"raw_size\": ${vsize:--1},
                \"raw_downloads\": ${vdownloads:--1},
                \"raw_downloads_month\": ${vdownloads_month:--1},
                \"raw_downloads_week\": ${vdownloads_week:--1},
                \"raw_downloads_day\": ${vdownloads_day:--1},
                \"tags\": [\"${vtags//,/\",\"}\"]
                }," >>"$BKG_INDEX_DIR/$owner/$repo/$package".json
        done
    fi

    # remove the last comma
    sed -i '$ s/,$//' "$BKG_INDEX_DIR/$owner/$repo/$package".json
    echo "]
    }" >>"$BKG_INDEX_DIR/$owner/$repo/$package".json

    # if the json is over 50MB, remove oldest versions from the packages with the most versions
    json_size=$(stat -c %s "$BKG_INDEX_DIR/$owner/$repo/$package".json)
    while [ "$json_size" -ge 50000000 ]; do
        jq -e 'map(.version | length > 0) | any' "$BKG_INDEX_DIR/$owner/$repo/$package".json || break
        jq -c 'sort_by(.versions | tonumber) | reverse | map(select(.versions > 0)) | map(.version |= sort_by(.id | tonumber) | del(.version[0]))' "$BKG_INDEX_DIR/$owner/$repo/$package".json >"$BKG_INDEX_DIR"/"$owner".tmp.json
        mv "$BKG_INDEX_DIR"/"$owner".tmp.json "$BKG_INDEX_DIR/$owner/$repo/$package".json
        json_size=$(stat -c %s "$BKG_INDEX_DIR/$owner/$repo/$package".json)
    done
}

refresh_owner() {
    check_limit || return
    [ -n "$1" ] || return
    echo "Refreshing $1..."
    [ -d "$BKG_INDEX_DIR" ] || mkdir "$BKG_INDEX_DIR"
    [ -d "$BKG_INDEX_DIR/$1" ] || mkdir "$BKG_INDEX_DIR/$1"
    run_parallel refresh_package "$(sqlite3 "$BKG_INDEX_DB" "select * from '$BKG_INDEX_TBL_PKG' where owner='$1' order by package;")"
    echo "Refreshed $1"
}

set_up() {
    printf -v MAX %x -1 && printf -v MAX %d 0x"${MAX/f/7}"
    set_BKG BKG_MAX "$MAX"
    set_BKG BKG_TIMEOUT "0"
    set_BKG BKG_TODAY "$(date -u +%Y-%m-%d)"
    set_BKG BKG_SCRIPT_START "$(date -u +%s)"
    set_BKG BKG_TIDY "1"

    if [ ! -f "$BKG_INDEX_DB" ]; then
        command curl -sSLNZO "https://github.com/$GITHUB_OWNER/$GITHUB_REPO/releases/latest/download/$BKG_INDEX_SQL.zst"
        unzstd --no-progress "$BKG_INDEX_SQL.zst" | sqlite3 "$BKG_INDEX_DB"
    fi

    [ -f "$BKG_INDEX_DB" ] || sqlite3 "$BKG_INDEX_DB" ""
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

    # copy table to a temp table to alter primary key
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
        primary key (owner_id, package, date)
    ); pragma auto_vacuum = full;"
    sqlite3 "$BKG_INDEX_DB" "$table_pkg_temp"
    sqlite3 "$BKG_INDEX_DB" "insert or ignore into '${BKG_INDEX_TBL_PKG}_temp' select * from '$BKG_INDEX_TBL_PKG';"
    sqlite3 "$BKG_INDEX_DB" "drop table '$BKG_INDEX_TBL_PKG';"
    sqlite3 "$BKG_INDEX_DB" "alter table '${BKG_INDEX_TBL_PKG}_temp' rename to '$BKG_INDEX_TBL_PKG';"
}

update_owners() {
    set_up
    set_BKG BKG_TIMEOUT "2"
    TODAY=$(get_BKG BKG_TODAY)
    [ -n "$(get_BKG BKG_BATCH_FIRST_STARTED)" ] || set_BKG BKG_BATCH_FIRST_STARTED "$TODAY"
    [[ "$1" != "0" ]] || get_BKG_set BKG_OWNERS_QUEUE | env_parallel --lb remove_owner
    [ -n "$(get_BKG BKG_OWNERS_QUEUE)" ] || set_BKG BKG_BATCH_FIRST_STARTED "$TODAY"
    [ -n "$(get_BKG BKG_RATE_LIMIT_START)" ] || set_BKG BKG_RATE_LIMIT_START "$(date -u +%s)"
    [ -n "$(get_BKG BKG_CALLS_TO_API)" ] || set_BKG BKG_CALLS_TO_API "0"
    BKG_BATCH_FIRST_STARTED=$(get_BKG BKG_BATCH_FIRST_STARTED)

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

    # if this is a scheduled update, scrape all owners that haven't been scraped in this batch
    if [ "$1" = "0" ]; then
        [ -n "$(get_BKG BKG_LAST_SCANNED_ID)" ] || set_BKG BKG_LAST_SCANNED_ID "0"
        [ -s "$BKG_OWNERS" ] || seq 1 10 | env_parallel --lb page_owner
        sqlite3 "$BKG_INDEX_DB" "select owner_id, owner from '$BKG_INDEX_TBL_PKG' where date not between date('$BKG_BATCH_FIRST_STARTED') and date('$TODAY') group by owner_id;" | awk -F'|' '{print $1"/"$2}' | env_parallel --lb add_owner
    fi

    # add more owners
    if [ -s "$BKG_OWNERS" ]; then
        sed -i '/^\s*$/d' "$BKG_OWNERS"
        echo >>"$BKG_OWNERS"
        awk 'NF' "$BKG_OWNERS" >owners.tmp && mv owners.tmp "$BKG_OWNERS"
        sed -i 's/^[[:space:]]*//;s/[[:space:]]*$//' "$BKG_OWNERS"
        env_parallel --lb add_owner <"$BKG_OWNERS"
        echo >"$BKG_OWNERS"
    fi

    get_BKG_set BKG_OWNERS_QUEUE | env_parallel --lb update_owner
    clean_up
    printf "CHANGELOG.md\n*.json\nindex.sql*\n" >../.gitignore
}

refresh_owners() {
    set_up
    sqlite3 "$BKG_INDEX_DB" "select distinct owner from '$BKG_INDEX_TBL_PKG';" | env_parallel --lb refresh_owner
    [ ! -f ../.gitignore ] || git rm ../.gitignore
}
