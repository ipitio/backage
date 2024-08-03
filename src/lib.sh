#!/bin/bash
# Backage library
# Usage: ./lib.sh
# Dependencies: curl, jq, sqlite3, zstd, parallel
# Copyright (c) ipitio
#
# shellcheck disable=SC1090,SC1091,SC2015,SC2034

if ! command -v curl &>/dev/null || ! command -v jq &>/dev/null || ! command -v sqlite3 &>/dev/null || ! command -v zstd &>/dev/null || ! command -v parallel &>/dev/null; then
    echo "Installing dependencies..."
    sudo apt-get update
    sudo apt-get install curl jq parallel sqlite3 zstd -y
    echo "Dependencies installed"
fi

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
PRAGMA synchronous = OFF;
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = MEMORY;
PRAGMA locking_mode = EXCLUSIVE;
PRAGMA cache_size = -500000;
.output stdout
") "$@" 2>/dev/null
}

get_BKG() {
    local res=""

    while ! ln "$BKG_ENV" "$BKG_ENV.lock" 2>/dev/null; do
        sleep 0.1
    done

    ! grep -q "^$1=" "$BKG_ENV" || res=$(grep "^$1=" "$BKG_ENV" | cut -d'=' -f2)
    rm -f "$BKG_ENV.lock"
    echo "$res"
}

set_BKG() {
    local value
    local tmp_file
    value=$(echo "$2" | perl -pe 'chomp if eof')
    tmp_file=$(mktemp)

    while ! ln "$BKG_ENV" "$BKG_ENV.lock" 2>/dev/null; do
        sleep 0.1
    done

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
    list=$(get_BKG_set "$1" | awk '!seen[$0]++' | perl -pe 's/\n/\\n/g')
    # shellcheck disable=SC2076
    [[ "$list" =~ "$2" ]] && code=1 || list="${list:+$list\n}$2"
    set_BKG "$1" "$(echo "$list" | perl -pe 's/\\n/\n/g' | perl -pe 's/\n/\\n/g')"
    return $code
}

del_BKG_set() {
    local list
    list=$(get_BKG_set "$1" | grep -v "$2")
    set_BKG "$1" "$(echo "$list" | perl -pe 's/\\n/\n/g' | perl -pe 's/\n/\\n/g' | perl -pe 's/^\\n//' | perl -pe 's/\\n$//' | perl -pe 's/\\n\\n/\\n/')"
}

del_BKG() {
    local tmp_file
    tmp_file=$(mktemp)

    while ! ln "$BKG_ENV" "$BKG_ENV.lock" 2>/dev/null; do
        sleep 0.1
    done

    grep -v "^$1=" "$BKG_ENV" >"$tmp_file"
    mv "$tmp_file" "$BKG_ENV"
    rm -f "$BKG_ENV.lock"
}

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
    [[ $(get_BKG BKG_AUTO) -eq 0 || $max_len -lt 3600 ]] || max_len=3600

    if ((script_limit_diff >= max_len)); then
        if (($(get_BKG BKG_TIMEOUT) == 0)); then
            set_BKG BKG_TIMEOUT "1"
            echo "Stopping $$..."
        fi

        return 3
    fi

    # wait if 1000 or more calls have been made in the last hour
    rate_limit_diff=$((rate_limit_end - $(get_BKG BKG_RATE_LIMIT_START)))
    hours_passed=$((rate_limit_diff / 3600))

    if ((total_calls >= 1000 * (hours_passed + 1))); then
        echo "$total_calls calls to the GitHub API in $((rate_limit_diff / 60)) minutes"
        remaining_time=$((3600 * (hours_passed + 1) - rate_limit_diff))
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

    ( # parallel --lb --halt soon,fail=1
        local i=0

        for j in $2; do
            code=$(cat "$exit_code")
            ! grep -q "3" <<<"$code" || exit
            ! grep -q "2" <<<"$code" || break
            ((i++))
            ("$1" "$j" || echo "$?" >>"$exit_code") &

            if ((i >= $(nproc))); then
                wait
                i=0
            fi
        done
    ) &

    wait "$!"
    code=$(cat "$exit_code")
    rm -f "$exit_code"
    ! grep -q "3" <<<"$code" || return 3
}

_jq() {
    echo "$1" | base64 --decode | jq -r "${@:2}"
}

save_version() {
    check_limit || return $?
    [ -n "$1" ] || return
    local id
    local name
    local tags
    local versions_json
    id=$(_jq "$1" '.id')
    name=$(_jq "$1" '.name')
    tags=$(_jq "$1" '.. | try .tags | join(",")')
    [ -n "$tags" ] || tags=$(_jq "$1" '.. | try .tags')
    versions_json=$(get_BKG BKG_VERSIONS_JSON_"${owner}_${package}")

    if [ -z "$versions_json" ] || ! jq -e . <<<"$versions_json" &>/dev/null; then
        versions_json="[]"
    fi

    if [ -n "$(jq -e '.[] | select(.id == "'"$id"'")' <<<"$versions_json")" ]; then
        versions_json=$(jq '.[] | if .id == "'"$id"'" then .name = "'"$name"'" | .tags = "'"$tags"'" else . end' <<<"$versions_json")
    else
        versions_json=$(jq '. + [{"id":"'"$id"'","name":"'"$name"'","tags":"'"$tags"'"}]' <<<"$versions_json")
    fi

    set_BKG BKG_VERSIONS_JSON_"${owner}_${package}" "$versions_json"
}

page_version() {
    check_limit || return $?
    [ -n "$1" ] || return
    local versions_json_more="[]"
    local calls_to_api
    local min_calls_to_api

    if [ -n "$GITHUB_TOKEN" ]; then
        echo "Starting $owner/$package page $1..."
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
    jq -e '.[].name' <<<"$versions_json_more" &>/dev/null || return 2
    local version_lines
    version_lines=$(jq -r '.[] | @base64' <<<"$versions_json_more")
    run_parallel save_version "$version_lines" || return $?
    echo "Started $owner/$package page $1"
    # if there are fewer than 100 lines, break
    [ "$(wc -l <<<"$version_lines")" -lt 100 ] || return 2
}

update_version() {
    check_limit || return $?
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
    version_id=$(_jq "$1" '.id')
    version_name=$(_jq "$1" '.name')
    version_tags=$(_jq "$1" '.tags')
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
    [[ "$(sqlite3 "$BKG_INDEX_DB" "select count(*) from '$table_version_name' where id='$version_id' and date >= '$BKG_BATCH_FIRST_STARTED';")" =~ ^0*$ || "$owner" == "arevindh" ]] || return

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
    version_raw_downloads=$(echo "$version_html" | grep -Pzo 'Total downloads<[^<]*<[^<]*' | grep -Pzo '\d*$' | tr -d '\0' | tr -d ',')

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

    sqlite3 "$BKG_INDEX_DB" "insert or replace into '$table_version_name' (id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('$version_id', '$version_name', '$version_size', '$version_raw_downloads', '$version_raw_downloads_month', '$version_raw_downloads_week', '$version_raw_downloads_day', '$BKG_TODAY', '$version_tags');"
}

refresh_version() {
    check_limit 21500 || return $?
    [ -n "$1" ] || return
    IFS='|' read -r vid vname vsize vdownloads vdownloads_month vdownloads_week vdownloads_day vdate vtags <<<"$1"
    echo "{
        \"id\": ${vid:--1},
        \"name\": \"$vname\",
        \"date\": \"$vdate\",
        \"newest\": $([ "${vid:--1}" = "${version_newest_id:--1}" ] && echo "true" || echo "false"),
        \"size\": \"$(numfmt_size <<<"${vsize:--1}")\",
        \"downloads\": \"$(numfmt <<<"${vdownloads:--1}")\",
        \"downloads_month\": \"$(numfmt <<<"${vdownloads_month:--1}")\",
        \"downloads_week\": \"$(numfmt <<<"${vdownloads_week:--1}")\",
        \"downloads_day\": \"$(numfmt <<<"${vdownloads_day:--1}")\",
        \"raw_size\": ${vsize:--1},
        \"raw_downloads\": ${vdownloads:--1},
        \"raw_downloads_month\": ${vdownloads_month:--1},
        \"raw_downloads_week\": ${vdownloads_week:--1},
        \"raw_downloads_day\": ${vdownloads_day:--1},
        \"tags\": [\"${vtags//,/\",\"}\"]
    }," >>"$json_file.$vid"
}

save_package() {
    check_limit || return $?
    [ -n "$1" ] || return
    local package_new
    local package_type
    local repo
    local packages
    package_new=$(cut -d'/' -f7 <<<"$1" | tr -d '"')
    package_new=${package_new%/}
    [ -n "$package_new" ] || return
    package_type=$(cut -d'/' -f5 <<<"$1")
    repo=$(grep -zoP '(?<=href="/'"$owner_type"'/'"$owner"'/packages/'"$package_type"'/package/'"$package_new"'")(.|\n)*?href="/'"$owner"'/[^"]+"' <<<"$html" | tr -d '\0' | grep -oP 'href="/'"$owner"'/[^"]+' | cut -d'/' -f3)
    package_type=${package_type%/}
    repo=${repo%/}
    set_BKG_set BKG_PACKAGES_"$owner" "$package_type/$repo/$package_new"
}

page_package() {
    check_limit || return $?
    [ -n "$1" ] || return
    local packages_lines
    local html
    echo "Starting $owner page $1..."
    [ "$owner_type" = "users" ] && html=$(curl "https://github.com/$owner?tab=packages&visibility=public&&per_page=100&page=$1") || html=$(curl "https://github.com/$owner_type/$owner/packages?visibility=public&per_page=100&page=$1")
    packages_lines=$(grep -zoP 'href="/'"$owner_type"'/'"$owner"'/packages/[^/]+/package/[^"]+"' <<<"$html" | tr -d '\0')

    if [ -z "$packages_lines" ]; then
        sed -i '/^'"$owner"'$/d' "$BKG_OWNERS"
        sed -i '/^'"$owner_id"'\/'"$owner"'$/d' "$BKG_OWNERS"
        return 2
    fi

    packages_lines=${packages_lines//href=/\\nhref=}
    packages_lines=${packages_lines//\\n/$'\n'} # replace \n with newline
    run_parallel save_package "$packages_lines" || return $?
    echo "Started $owner page $1"
    # if there are fewer than 100 lines, break
    [ "$(wc -l <<<"$packages_lines")" -lt 100 ] || return 2
}

update_package() {
    check_limit || return $?
    [ -n "$1" ] || return
    package_type=$(cut -d'/' -f1 <<<"$1")
    repo=$(cut -d'/' -f2 <<<"$1")
    package=$(cut -d'/' -f3 <<<"$1")
    package=${package%/}

    if grep -q "$owner/$repo/$package" "$BKG_OPTOUT"; then
        sqlite3 "$BKG_INDEX_DB" "delete from '$BKG_INDEX_TBL_PKG' where owner_id='$owner_id' and package='$package';"
        sqlite3 "$BKG_INDEX_DB" "drop table if exists '${BKG_INDEX_TBL_VER}_${owner_type}_${package_type}_${owner}_${repo}_${package}';"
        return
    fi

    [[ "$(sqlite3 "$BKG_INDEX_DB" "select count(*) from '$BKG_INDEX_TBL_PKG' where owner_id='$owner_id' and package='$package' and date >= '$BKG_BATCH_FIRST_STARTED';")" =~ ^0*$ || "$owner" == "arevindh" ]] || return
    local html
    local query
    local raw_downloads=-1
    local raw_downloads_month=-1
    local raw_downloads_week=-1
    local raw_downloads_day=-1
    local size=-1
    local versions_all_ids=""
    local versions_json=""

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
    echo "Updating $owner/$package..."
    raw_downloads=$(grep -Pzo 'Total downloads[^"]*"\d*' <<<"$html" | grep -Pzo '\d*$' | tr -d '\0') # https://stackoverflow.com/a/74214537
    [[ "$raw_downloads" =~ ^[0-9]+$ ]] || raw_downloads=-1
    table_version_name="${BKG_INDEX_TBL_VER}_${owner_type}_${package_type}_${owner}_${repo}_${package}"

    if [ -n "$(sqlite3 "$BKG_INDEX_DB" "select name from sqlite_master where type='table' and name='$table_version_name';")" ]; then
        run_parallel save_version "$(sqlite3 -json "$BKG_INDEX_DB" "select id, name, tags from '$table_version_name' group by id order by date desc;" | jq -r '.[] | @base64')" || return $?
    fi

    for page in $(seq 1 100); do
        local pages_left=0
        set_BKG BKG_VERSIONS_JSON_"${owner}_${package}" "[]"
        page_version "$page"
        pages_left=$?
        ((pages_left != 3)) || return 3
        versions_json=$(get_BKG BKG_VERSIONS_JSON_"${owner}_${package}")
        jq -e . <<<"$versions_json" &>/dev/null || versions_json="[{\"id\":\"latest\",\"name\":\"latest\",\"tags\":\"\"}]"
        del_BKG BKG_VERSIONS_JSON_"${owner}_${package}"
        versions_all_ids=$(jq -r '.[] | .id' <<<"$versions_json" | sort -u)
        [ "$versions_all_ids" = "$(sqlite3 "$BKG_INDEX_DB" "select distinct id from '$table_version_name' where date >= '$BKG_BATCH_FIRST_STARTED';")" ] || { run_parallel update_version "$(jq -r '.[] | @base64' <<<"$versions_json")" || return $?; }
        ((pages_left != 2)) || break
    done

    # calculate the overall downloads and size
    if [ -n "$(sqlite3 "$BKG_INDEX_DB" "select name from sqlite_master where type='table' and name='$table_version_name';")" ]; then
        max_date=$(sqlite3 "$BKG_INDEX_DB" "select date from '$table_version_name' order by date desc limit 1;")
        query="select sum(downloads), sum(downloads_month), sum(downloads_week), sum(downloads_day) from '$table_version_name' where date='$max_date';"
        summed_raw_downloads=$(sqlite3 "$BKG_INDEX_DB" "$query" | cut -d'|' -f1)
        raw_downloads_month=$(sqlite3 "$BKG_INDEX_DB" "$query" | cut -d'|' -f2)
        raw_downloads_week=$(sqlite3 "$BKG_INDEX_DB" "$query" | cut -d'|' -f3)
        raw_downloads_day=$(sqlite3 "$BKG_INDEX_DB" "$query" | cut -d'|' -f4)
        [[ "$summed_raw_downloads" =~ ^[0-9]+$ ]] && ((summed_raw_downloads > raw_downloads)) && raw_downloads=$summed_raw_downloads || :
        version_newest_id=$(sqlite3 "$BKG_INDEX_DB" "select id from '${BKG_INDEX_TBL_VER}_${owner_type}_${package_type}_${owner}_${repo}_${package}' order by id desc limit 1;")
        size=$(sqlite3 "$BKG_INDEX_DB" "select size from '${BKG_INDEX_TBL_VER}_${owner_type}_${package_type}_${owner}_${repo}_${package}' where id='$version_newest_id' order by date desc limit 1;")
    fi

    sqlite3 "$BKG_INDEX_DB" "insert or replace into '$BKG_INDEX_TBL_PKG' (owner_id, owner_type, package_type, owner, repo, package, downloads, downloads_month, downloads_week, downloads_day, size, date) values ('$owner_id', '$owner_type', '$package_type', '$owner', '$repo', '$package', '$raw_downloads', '$raw_downloads_month', '$raw_downloads_week', '$raw_downloads_day', '$size', '$BKG_BATCH_FIRST_STARTED');"
    echo "Updated $owner/$package"
}

refresh_package() {
    check_limit 21500 || return $?
    [ -n "$1" ] || return
    local version_count
    local version_with_tag_count
    IFS='|' read -r owner_id owner_type package_type owner repo package downloads downloads_month downloads_week downloads_day size date tags <<<"$1"
    max_date=$(sqlite3 "$BKG_INDEX_DB" "select date from '$BKG_INDEX_TBL_PKG' where owner_id='$owner_id' and package='$package' order by date desc limit 1;")
    [ "$date" = "$max_date" ] || return
    echo "Refreshing $owner/$package..."
    json_file="$BKG_INDEX_DIR/$owner/$repo/$package.json"
    [ -d "$BKG_INDEX_DIR/$owner/$repo" ] || mkdir "$BKG_INDEX_DIR/$owner/$repo"
    version_count=0
    version_with_tag_count=0
    table_version_name="${BKG_INDEX_TBL_VER}_${owner_type}_${package_type}_${owner}_${repo}_${package}"

    if [ -n "$(sqlite3 "$BKG_INDEX_DB" "select name from sqlite_master where type='table' and name='$table_version_name';")" ]; then
        version_count=$(sqlite3 "$BKG_INDEX_DB" "select count(distinct id) from '$table_version_name';")
        version_with_tag_count=$(sqlite3 "$BKG_INDEX_DB" "select count(distinct id) from '$table_version_name' where tags != '' and tags is not null;")
    fi

    echo "{
        \"owner_type\": \"$owner_type\",
        \"package_type\": \"$package_type\",
        \"owner_id\": \"$owner_id\",
        \"owner\": \"$owner\",
        \"repo\": \"$repo\",
        \"package\": \"$package\",
        \"date\": \"$(sqlite3 "$BKG_INDEX_DB" "select date from '$table_version_name' order by date desc limit 1;")\",
        \"size\": \"$(numfmt_size <<<"${size:--1}")\",
        \"versions\": \"$(numfmt <<<"${version_count:--1}")\",
        \"tagged\": \"$(numfmt <<<"${version_with_tag_count:--1}")\",
        \"downloads\": \"$(numfmt <<<"${downloads:--1}")\",
        \"downloads_month\": \"$(numfmt <<<"${downloads_month:--1}")\",
        \"downloads_week\": \"$(numfmt <<<"${downloads_week:--1}")\",
        \"downloads_day\": \"$(numfmt <<<"${downloads_day:--1}")\",
        \"raw_size\": ${size:--1},
        \"raw_versions\": ${version_count:--1},
        \"raw_tagged\": ${version_with_tag_count:--1},
        \"raw_downloads\": ${downloads:--1},
        \"raw_downloads_month\": ${downloads_month:--1},
        \"raw_downloads_week\": ${downloads_week:--1},
        \"raw_downloads_day\": ${downloads_day:--1},
        \"version\":
    [" >"$json_file"

    # add the versions to index/"$owner".json
    if [ "${version_count:--1}" -gt 0 ]; then
        version_newest_id=$(sqlite3 "$BKG_INDEX_DB" "select id from '$table_version_name' order by id desc limit 1;")
        rm -f "$json_file".*
        run_parallel refresh_version "$(sqlite3 "$BKG_INDEX_DB" "select * from '$table_version_name' where date >= '$BKG_BATCH_FIRST_STARTED' group by id;")"
    fi

    # use find to check if the files exist
    if find "$json_file".* -type f -quit 2>/dev/null; then
        cat "$json_file".* >>"$json_file"
        rm -f "$json_file".*
    else
        echo "{
            \"id\": -1,
            \"name\": \"latest\",
            \"date\": \"$date\",
            \"newest\": true,
            \"size\": \"$(numfmt_size <<<"${size:--1}")\",
            \"downloads\": \"$(numfmt <<<"${downloads:--1}")\",
            \"downloads_month\": \"$(numfmt <<<"${downloads_month:--1}")\",
            \"downloads_week\": \"$(numfmt <<<"${downloads_week:--1}")\",
            \"downloads_day\": \"$(numfmt <<<"${downloads_day:--1}")\",
            \"raw_size\": ${size:--1},
            \"raw_downloads\": ${downloads:--1},
            \"raw_downloads_month\": ${downloads_month:--1},
            \"raw_downloads_week\": ${downloads_week:--1},
            \"raw_downloads_day\": ${downloads_day:--1},
            \"tags\": [\"\"]
            }," >>"$json_file"
    fi

    # remove the last comma
    sed -i '$ s/,$//' "$json_file"
    echo "]}" >>"$json_file"
    jq -c . "$json_file" >"$json_file".tmp.json 2>/dev/null
    [ ! -f "$json_file".tmp.json ] || mv "$json_file".tmp.json "$json_file"
    local json_size
    json_size=$(stat -c %s "$json_file")

    # if the json is over 50MB, remove oldest versions from the packages with the most versions
    if jq -e . <<<"$(cat "$json_file")" &>/dev/null; then
        while [ "$json_size" -ge 50000000 ]; do
            jq -e 'map(.version | length > 0) | any' "$json_file" || break
            jq -c 'sort_by(.versions | tonumber) | reverse | map(select(.versions > 0)) | map(.version |= sort_by(.id | tonumber) | del(.version[0]))' "$json_file" >"$json_file".tmp.json
            mv "$json_file".tmp.json "$json_file"
            json_size=$(stat -c %s "$json_file")
        done
    elif [ "$json_size" -ge 100000000 ]; then
        rm -f "$json_file"
    fi

    echo "Refreshed $owner/$package"
}

request_owner() {
    check_limit || return $?
    [ -n "$1" ] || return
    local owner
    local id
    owner=$(_jq "$1" '.login')
    id=$(_jq "$1" '.id')

    while ! ln "$BKG_OWNERS" "$BKG_OWNERS.lock" 2>/dev/null; do
        sleep 0.1
    done

    grep -q "^.*\/*$owner$" "$BKG_OWNERS" || echo "$id/$owner" >>"$BKG_OWNERS"
    local return_code=0

    if [ "$(stat -c %s "$BKG_OWNERS")" -ge 100000000 ]; then
        sed -i '$d' "$BKG_OWNERS"
        return_code=2
    else
        set_BKG BKG_LAST_SCANNED_ID "$id"
    fi

    rm -f "$BKG_OWNERS.lock"
    return $return_code
}

save_owner() {
    check_limit || return $?
    owner=$(echo "$1" | tr -d '[:space:]')
    [ -n "$owner" ] || return
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
}

page_owner() {
    check_limit || return $?
    [ -n "$1" ] || return
    local owners_more="[]"
    local calls_to_api
    local min_calls_to_api

    if [ -n "$GITHUB_TOKEN" ]; then
        echo "Checking owners page $1..."
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
    jq -e '.[].login' <<<"$owners_more" &>/dev/null || return 2
    local owners_lines
    owners_lines=$(jq -r '.[] | @base64' <<<"$owners_more")
    run_parallel request_owner "$owners_lines" || return $?
    echo "Checked owners page $1"
    # if there are fewer than 100 lines, break
    [ "$(wc -l <<<"$owners_lines")" -lt 100 ] || return 2
}

update_owner() {
    check_limit || return $?
    [ -n "$1" ] || return
    owner=$(cut -d'/' -f2 <<<"$1")
    owner_id=$(cut -d'/' -f1 <<<"$1")
    echo "Updating $owner..."
    [ -n "$(curl "https://github.com/orgs/$owner/people" | grep -zoP 'href="/orgs/'"$owner"'/people"' | tr -d '\0')" ] && owner_type="orgs" || owner_type="users"

    for page in $(seq 1 100); do
        local pages_left=0
        set_BKG BKG_PACKAGES_"$owner" ""
        page_package "$page"
        pages_left=$?
        ((pages_left != 3)) || return 3
        run_parallel update_package "$(get_BKG_set BKG_PACKAGES_"$owner")" || return $?
        del_BKG BKG_PACKAGES_"$owner"
        ((pages_left != 2)) || break
    done

    echo "Updated $owner"
}

refresh_owner() {
    check_limit 21500 || return $?
    [ -n "$1" ] || return
    echo "Refreshing $1..."
    [ -d "$BKG_INDEX_DIR" ] || mkdir "$BKG_INDEX_DIR"
    [ -d "$BKG_INDEX_DIR/$1" ] || mkdir "$BKG_INDEX_DIR/$1"
    run_parallel refresh_package "$(sqlite3 "$BKG_INDEX_DB" "select * from '$BKG_INDEX_TBL_PKG' where owner='$1' and date >= '$BKG_BATCH_FIRST_STARTED' group by package;")"
    echo "Refreshed $1"
}

set_up() {
    set_BKG BKG_TIMEOUT "0"
    set_BKG BKG_TODAY "$(date -u +%Y-%m-%d)"
    set_BKG BKG_SCRIPT_START "$(date -u +%s)"
    set_BKG BKG_AUTO "${1:-0}"
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

    # copy table to a temp table to alter primary key
    local table_pkg_temp="create table if not exists '${BKG_INDEX_TBL_PKG}_temp' (
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
    sqlite3 "$BKG_INDEX_DB" "insert or replace into '${BKG_INDEX_TBL_PKG}_temp' select * from '$BKG_INDEX_TBL_PKG';"
    sqlite3 "$BKG_INDEX_DB" "drop table '$BKG_INDEX_TBL_PKG';"
    sqlite3 "$BKG_INDEX_DB" "alter table '${BKG_INDEX_TBL_PKG}_temp' rename to '$BKG_INDEX_TBL_PKG';"
}

clean_up() {
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

update_owners() {
    local mode=-1

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

    set_up "$mode"
    [ -n "$(get_BKG BKG_LAST_SCANNED_ID)" ] || set_BKG BKG_LAST_SCANNED_ID "0"
    local owners_to_update
    local rotated=false
    local query
    local tables
    local owners
    local repos
    local packages
    sqlite3 "$BKG_INDEX_DB" "select owner_id, owner, package from '$BKG_INDEX_TBL_PKG' where date >= '$BKG_BATCH_FIRST_STARTED' group by owner_id, owner, package;" | sort -u >packages_already_updated
    sqlite3 "$BKG_INDEX_DB" "select owner_id, owner, package from '$BKG_INDEX_TBL_PKG' group by owner_id, owner, package;" | sort -u >packages_all
    set_BKG BKG_OWNERS_QUEUE ""

    # if this is a scheduled update, scrape all owners
    if [ "$mode" -eq 0 ]; then
        comm -13 packages_already_updated packages_all >packages_to_update
        echo "all: $(wc -l <packages_all)"
        echo "done: $(wc -l <packages_already_updated)"
        echo "left: $(wc -l <packages_to_update)"
        awk -F'|' '{print $1"/"$2}' <packages_to_update | sort -u | env_parallel --lb save_owner

        if [ -z "$(get_BKG_set BKG_OWNERS_QUEUE)" ]; then
            set_BKG BKG_BATCH_FIRST_STARTED "$BKG_TODAY"
            [ -s "$BKG_OWNERS" ] || seq 1 10 | env_parallel --lb --halt soon,fail=1 page_owner
            awk -F'|' '{print $1"/"$2}' <packages_all | sort -u | env_parallel --lb save_owner
        else
            [ -n "$(get_BKG BKG_BATCH_FIRST_STARTED)" ] || set_BKG BKG_BATCH_FIRST_STARTED "$BKG_TODAY"
        fi
    elif [ "$mode" -eq 1 ]; then
        owners_to_update="693151/arevindh"
    fi

    # add more owners
    if [ -s "$BKG_OWNERS" ]; then
        sed -i '/^\s*$/d' "$BKG_OWNERS"
        echo >>"$BKG_OWNERS"
        awk 'NF' "$BKG_OWNERS" >owners.tmp && mv owners.tmp "$BKG_OWNERS"
        sed -i 's/^[[:space:]]*//;s/[[:space:]]*$//' "$BKG_OWNERS"
        awk '!seen[$0]++' "$BKG_OWNERS" >owners.tmp && mv owners.tmp "$BKG_OWNERS"
        # remove lines from $BKG_OWNERS that are in $packages_all
        echo "$(
            awk -F'|' '{print $1"/"$2}' <packages_all
            awk -F'|' '{print $2}' <packages_all
        )" | sort -u | parallel "sed -i '\,^{}$,d' $BKG_OWNERS"
        local request_owners
        request_owners=$(cat "$BKG_OWNERS")
        owners_to_update=$request_owners${owners_to_update:+$owners_to_update}
        request_owners=""
    fi

    rm -f packages_already_updated packages_all packages_to_update
    BKG_BATCH_FIRST_STARTED=$(get_BKG BKG_BATCH_FIRST_STARTED)
    [ -z "$owners_to_update" ] || printf "%s\n" "${owners_to_update//\\n/$'\n'}" | env_parallel --lb save_owner
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

    echo "Updating templates..."
    [ ! -f $BKG_ROOT/CHANGELOG.md ] || rm -f $BKG_ROOT/CHANGELOG.md
    \cp templates/.CHANGELOG.md $BKG_ROOT/CHANGELOG.md
    owners=$(sqlite3 "$BKG_INDEX_DB" "select count(distinct owner_id) from '$BKG_INDEX_TBL_PKG';")
    repos=$(sqlite3 "$BKG_INDEX_DB" "select count(distinct repo) from '$BKG_INDEX_TBL_PKG';")
    packages=$(sqlite3 "$BKG_INDEX_DB" "select count(distinct package) from '$BKG_INDEX_TBL_PKG';")
    perl -0777 -pe 's/\[OWNERS\]/'"$owners"'/g; s/\[REPOS\]/'"$repos"'/g; s/\[PACKAGES\]/'"$packages"'/g' $BKG_ROOT/CHANGELOG.md >CHANGELOG.tmp && [ -f CHANGELOG.tmp ] && mv CHANGELOG.tmp $BKG_ROOT/CHANGELOG.md || :
    ! $rotated || echo " The database grew over 2GB and was rotated, but you can find all previous data under [Releases](https://github.com/$GITHUB_OWNER/$GITHUB_REPO/releases)." >>$BKG_ROOT/CHANGELOG.md
    [ ! -f $BKG_ROOT/README.md ] || rm -f $BKG_ROOT/README.md
    \cp templates/.README.md $BKG_ROOT/README.md
    perl -0777 -pe 's/<GITHUB_OWNER>/'"$GITHUB_OWNER"'/g; s/<GITHUB_REPO>/'"$GITHUB_REPO"'/g; s/<GITHUB_BRANCH>/'"$GITHUB_BRANCH"'/g' $BKG_ROOT/README.md >README.tmp && [ -f README.tmp ] && mv README.tmp $BKG_ROOT/README.md || :
    echo "Updated templates"
    clean_up
}

refresh_owners() {
    set_up "$@"
    sqlite3 "$BKG_INDEX_DB" "select distinct owner from '$BKG_INDEX_TBL_PKG' where date >= '$BKG_BATCH_FIRST_STARTED';" | env_parallel --lb refresh_owner
    clean_up
}

dldb() {
    # `cd src && source lib.sh && dldb` to dl the latest db
    [ ! -f "$BKG_INDEX_DB" ] || mv "$BKG_INDEX_DB" "$BKG_INDEX_DB".bak
    command curl -sSLNZ "https://github.com/ipitio/backage/releases/download/$(command curl -sSLNZ "https://github.com/ipitio/backage/releases/latest" | grep -oP 'href="/ipitio/backage/releases/tag/[^"]+' | cut -d'/' -f6)/index.sql.zst" | unzstd -v -c | sqlite3 "$BKG_INDEX_DB"
    [ ! -f "$BKG_INDEX_DB" ] || rm -f "$BKG_INDEX_DB".bak
    [ -f "$BKG_ROOT/.gitignore" ] || echo "index.db*" >>$BKG_ROOT/.gitignore
    grep -q "index.db" "$BKG_ROOT/.gitignore" || echo "index.db*" >>$BKG_ROOT/.gitignore
}
