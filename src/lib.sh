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

# format numbers like 1000 to 1k
numfmt() {
    awk '{ split("k M B T P E Z Y", v); s=0; while( $1>999.9 ) { $1/=1000; s++ } print int($1*10)/10 v[s] }'
}

# format bytes to KB, MB, GB, etc.
numfmt_size() {
    awk '{ split("kB MB GB TB PB EB ZB YB", v); s=0; while( $1>999.9 ) { $1/=1000; s++ } print int($1*10)/10 " " v[s] }' | sed 's/ //'
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
    local file=env.env
    local key=$1

    while ! ln "$file" "$file.lock" 2>/dev/null; do
        sleep 0.1
    done

    touch "$file.lock"
    grep -Po "(?<=^$key=).*" "$file" | tail -n 1
    rm "$file.lock"
}

set_BKG() {
    local file=env.env
    local key=$1
    local value=$2
    local tmp_file
    tmp_file=$(mktemp)

    while ! ln "$file" "$file.lock" 2>/dev/null; do
        sleep 0.1
    done

    touch "$file.lock"
    sed "s/^$key=.*/$key=$value/" "$file" >"$tmp_file"
    mv "$tmp_file" "$file"
    rm "$file.lock"
}

check_limit() {
    # exit if the script has been running for 5 hours
    total_calls=$(get_BKG BKG_CALLS_TO_API)
    rate_limit_end=$(date -u +%s)
    script_limit_diff=$((rate_limit_end - SCRIPT_START))
    ((script_limit_diff < 18000)) || { echo "Script has been running for 5 hours!" && return 1; }

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

xz_db() {
    [ -f "$BKG_INDEX_DB" ] || return 1
    rotated=false
    echo "Compressing the database..."
    sqlite3 "$BKG_INDEX_DB" ".dump" | zstd -22 --ultra --long -T0 -o "$BKG_INDEX_SQL".zst.new

    if [ -f "$BKG_INDEX_SQL".zst.new ]; then
        # rotate the database if it's greater than 2GB
        if [ "$(stat -c %s "$BKG_INDEX_SQL".zst.new)" -ge 2000000000 ]; then
            rotated=true
            echo "Rotating the database..."
            [ -d "$BKG_INDEX_SQL".d ] || mkdir "$BKG_INDEX_SQL".d
            [ ! -f "$BKG_INDEX_SQL".zst ] || mv "$BKG_INDEX_SQL".zst "$BKG_INDEX_SQL".d/"$(date -u +%Y.%m.%d)".zst
            query="delete from '$BKG_INDEX_TBL_PKG' where date not between date('$BKG_BATCH_FIRST_STARTED') and date('$TODAY');"
            sqlite3 "$BKG_INDEX_DB" "$query"
            query="select name from sqlite_master where type='table' and name like '${BKG_INDEX_TBL_VER}_%';"
            tables=$(sqlite3 "$BKG_INDEX_DB" "$query")

            for table in $tables; do
                query="delete from '$table' where date not between date('$BKG_BATCH_FIRST_STARTED') and date('$TODAY');"
                sqlite3 "$BKG_INDEX_DB" "$query"
            done

            sqlite3 "$BKG_INDEX_DB" "vacuum;"
            sqlite3 "$BKG_INDEX_DB" ".dump" | zstd -22 --ultra --long -T0 -o "$BKG_INDEX_SQL".zst.new
        fi

        mv "$BKG_INDEX_SQL".zst.new "$BKG_INDEX_SQL".zst
        echo "Compressed the database"
    else
        echo "Failed to compress the database!"
    fi

    # if the database is smaller than 1kb, return 1
    [ "$(stat -c %s "$BKG_INDEX_SQL".zst)" -ge 1000 ] || return 1
    echo "Updating the CHANGELOG..."
    [ ! -f ../CHANGELOG.md ] || rm -f ../CHANGELOG.md
    \cp ../templates/.CHANGELOG.md ../CHANGELOG.md
    query="select count(distinct owner_id) from '$BKG_INDEX_TBL_PKG';"
    owners=$(sqlite3 "$BKG_INDEX_DB" "$query")
    query="select count(distinct repo) from '$BKG_INDEX_TBL_PKG';"
    repos=$(sqlite3 "$BKG_INDEX_DB" "$query")
    query="select count(distinct package) from '$BKG_INDEX_TBL_PKG';"
    packages=$(sqlite3 "$BKG_INDEX_DB" "$query")
    perl -0777 -pe 's/\[OWNERS\]/'"$owners"'/g; s/\[REPOS\]/'"$repos"'/g; s/\[PACKAGES\]/'"$packages"'/g' ../CHANGELOG.md >CHANGELOG.tmp && [ -f CHANGELOG.tmp ] && mv CHANGELOG.tmp ../CHANGELOG.md || :
    ! $rotated || echo " The database grew over 2GB and was rotated, but you can find all previous data under [Releases](https://github.com/$GITHUB_OWNER/$GITHUB_REPO/releases)." >>../CHANGELOG.md
    echo "Updated the CHANGELOG"
    # if index db is greater than 100MB, remove it
    if [ "$(stat -c %s "$BKG_INDEX_DB")" -ge 100000000 ]; then
        echo "Removing the database..."
        rm -f "$BKG_INDEX_DB"
        [ ! -f ../index.json ] || rm -f ../index.json
        echo "Removed the database"
    fi
}

update_version() {
    check_limit || return
    v_obj=$1
    [ -n "$v_obj" ] || return

    _jq() {
        echo "$v_obj" | base64 --decode | jq -r "$@"
    }

    version_size=-1
    version_id=$(_jq '.id')
    version_name=$(_jq '.name')
    version_tags=$(_jq '.tags')
    echo "Started $owner/$package/$version_id..."
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

        query="insert or replace into '$table_version_name' (id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('$version_id', '$version_name', '$version_size', '$version_raw_downloads', '$version_raw_downloads_month', '$version_raw_downloads_week', '$version_raw_downloads_day', '$TODAY', '$version_tags');"
        sqlite3 "$BKG_INDEX_DB" "$query"
    fi
    echo "Finished $owner/$package/$version_id"
}

update_package() {
    check_limit || return
    packages_line=$1
    [ -n "$packages_line" ] || return
    package_type=$(cut -d'/' -f1 <<<"$packages_line")
    repo=$(cut -d'/' -f2 <<<"$packages_line")
    package=$(cut -d'/' -f3 <<<"$packages_line")

    # optout.txt has lines like "owner/repo/package"
    if [ -f "$BKG_OPTOUT" ]; then
        while IFS= read -r line; do
            [ -n "$line" ] || continue

            if [ "$line" = "$owner/$repo/$package" ]; then
                # remove the package from the db
                query="delete from '$BKG_INDEX_TBL_PKG' where owner_id='$owner_id' and package='$package';"
                sqlite3 "$BKG_INDEX_DB" "$query"
                table_version_name="${BKG_INDEX_TBL_VER}_${owner_type}_${package_type}_${owner}_${repo}_${package}"
                query="drop table if exists '$table_version_name';"
                sqlite3 "$BKG_INDEX_DB" "$query"
                return
            fi
        done <"$BKG_OPTOUT"
    fi

    # manual update: skip if the package is already in the index; the rest are updated daily
    if [ "$1" = "1" ] && [[ "$owner" != "arevindh" ]]; then
        query="select count(*) from '$BKG_INDEX_TBL_PKG' where owner_id='$owner_id' and package='$package';"
        count=$(sqlite3 "$BKG_INDEX_DB" "$query")
        [[ "$count" =~ ^0*$ ]] || return
    fi

    # update stats
    query="select count(*) from '$BKG_INDEX_TBL_PKG' where owner_id='$owner_id' and package='$package' and date between date('$BKG_BATCH_FIRST_STARTED') and date('$TODAY');"
    count=$(sqlite3 "$BKG_INDEX_DB" "$query")
    echo "Getting versions for $owner/$package..."
    if [[ "$count" =~ ^0*$ || "$owner" == "arevindh" ]]; then
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
        is_public=$(grep -Pzo 'Total downloads' <<<"$html" | tr -d '\0')
        [ -n "$is_public" ] || return
        echo "Scraping $owner_type/$owner/$package_type/$repo/$package..."
        raw_downloads=$(grep -Pzo 'Total downloads[^"]*"\d*' <<<"$html" | grep -Pzo '\d*$' | tr -d '\0') # https://stackoverflow.com/a/74214537
        [[ "$raw_downloads" =~ ^[0-9]+$ ]] || raw_downloads=-1
        versions_json="[]"
        versions_page=0

        # add all the versions currently in the db to the versions_json, if they are not already there
        table_version_name="${BKG_INDEX_TBL_VER}_${owner_type}_${package_type}_${owner}_${repo}_${package}"
        query="select name from sqlite_master where type='table' and name='$table_version_name';"
        table_exists=$(sqlite3 "$BKG_INDEX_DB" "$query")

        if [ -n "$table_exists" ]; then
            query="select id, name, tags from '$table_version_name';"

            while IFS='|' read -r id name tags; do
                if ! jq -e ".[] | select(.id == \"$id\")" <<<"$versions_json" &>/dev/null; then
                    versions_json=$(jq ". += [{\"id\":\"$id\",\"name\":\"$name\",\"tags\":\"$tags\"}]" <<<"$versions_json")
                fi
            done < <(sqlite3 "$BKG_INDEX_DB" "$query")
        fi

        while true; do
            check_limit || return
            ((versions_page++))
            versions_json_more="[]"

            if [ -n "$GITHUB_TOKEN" ]; then
                versions_json_more=$(curl -H "Accept: application/vnd.github+json" \
                    -H "Authorization: Bearer $GITHUB_TOKEN" \
                    -H "X-GitHub-Api-Version: 2022-11-28" \
                    "https://api.github.com/$owner_type/$owner/packages/$package_type/$package/versions?per_page=$BKG_VERSIONS_PER_PAGE&page=$versions_page")
                # increment BKG_CALLS_TO_API in env.env
                calls_to_api=$(get_BKG BKG_CALLS_TO_API)
                min_calls_to_api=$(get_BKG BKG_MIN_CALLS_TO_API)
                ((calls_to_api++))
                ((min_calls_to_api++))
                set_BKG BKG_CALLS_TO_API "$calls_to_api"
                set_BKG BKG_MIN_CALLS_TO_API "$min_calls_to_api"
                jq -e . <<<"$versions_json_more" &>/dev/null || versions_json_more="[]"
            fi

            # if versions doesn't have .name, break
            jq -e '.[].name' <<<"$versions_json_more" &>/dev/null || break

            # add the new versions to the versions_json, if they are not already there
            for i in $(jq -r '.[] | @base64' <<<"$versions_json_more"); do
                _jq() {
                    echo "$i" | base64 --decode | jq -r "$@"
                }

                id=$(_jq '.id')
                name=$(_jq '.name')
                tags=$(_jq '.. | try .tags | join(",")')

                if ! jq -e ".[] | select(.id == \"$id\")" <<<"$versions_json" &>/dev/null; then
                    versions_json=$(jq ". += [{\"id\":\"$id\",\"name\":\"$name\",\"tags\":\"$tags\"}]" <<<"$versions_json")
                else
                    versions_json=$(jq "map(if .id == \"$id\" then .tags = \"$tags\" else . end)" <<<"$versions_json")
                fi
            done
        done
        echo "Got versions for $owner/$package"

        # scan the versions
        jq -e . <<<"$versions_json" &>/dev/null || versions_json="[{\"id\":\"latest\",\"name\":\"latest\"}]"
        #jq -r '.[] | @base64' <<<"$versions_json" | env_parallel -j 1000% --bar update_version >/dev/null
        echo "Scraping $owner/$package..."
        run_parallel update_version "$(jq -r '.[] | @base64' <<<"$versions_json")"
        echo "Scraped $owner/$package"
        # insert the package into the db
        if check_limit; then
            query="select name from sqlite_master where type='table' and name='$table_version_name';"
            table_exists=$(sqlite3 "$BKG_INDEX_DB" "$query")

            if [ -n "$table_exists" ]; then
                # calculate the total downloads
                query="select max(date) from '$table_version_name';"
                max_date=$(sqlite3 "$BKG_INDEX_DB" "$query")
                query="select sum(downloads), sum(downloads_month), sum(downloads_week), sum(downloads_day) from '$table_version_name' where date='$max_date';"
                # summed_raw_downloads=$(sqlite3 "$BKG_INDEX_DB" "$query" | cut -d'|' -f1)
                raw_downloads_month=$(sqlite3 "$BKG_INDEX_DB" "$query" | cut -d'|' -f2)
                raw_downloads_week=$(sqlite3 "$BKG_INDEX_DB" "$query" | cut -d'|' -f3)
                raw_downloads_day=$(sqlite3 "$BKG_INDEX_DB" "$query" | cut -d'|' -f4)

                # use the latest version's size as the package size
                query="select id from '${BKG_INDEX_TBL_VER}_${owner_type}_${package_type}_${owner}_${repo}_${package}' order by id desc limit 1;"
                version_newest_id=$(sqlite3 "$BKG_INDEX_DB" "$query")
                query="select size from '${BKG_INDEX_TBL_VER}_${owner_type}_${package_type}_${owner}_${repo}_${package}' where id='$version_newest_id' order by date desc limit 1;"
                size=$(sqlite3 "$BKG_INDEX_DB" "$query")
            fi

            query="insert or replace into '$BKG_INDEX_TBL_PKG' (owner_id, owner_type, package_type, owner, repo, package, downloads, downloads_month, downloads_week, downloads_day, size, date) values ('$owner_id', '$owner_type', '$package_type', '$owner', '$repo', '$package', '$raw_downloads', '$raw_downloads_month', '$raw_downloads_week', '$raw_downloads_day', '$size', '$TODAY');"
            sqlite3 "$BKG_INDEX_DB" "$query"
        fi
    fi
}

update_owner() {
    check_limit || return
    login_id=$1
    [ -n "$login_id" ] || return
    owner=$(cut -d'/' -f2 <<<"$login_id")
    echo "Processing $owner..."
    owner_id=$(cut -d'/' -f1 <<<"$login_id")
    owner_type="orgs"
    html=$(curl "https://github.com/orgs/$owner/people")
    is_org=$(grep -zoP 'href="/orgs/'"$owner"'/people"' <<<"$html" | tr -d '\0')
    [ -n "$is_org" ] || owner_type="users"
    packages=""
    packages_page=0
    # get the packages
    while [ "$packages_page" -le 100 ]; do
        check_limit || return
        ((packages_page++))

        if [ "$owner_type" = "orgs" ]; then
            html=$(curl "https://github.com/$owner_type/$owner/packages?visibility=public&per_page=100&page=$packages_page")
        else
            html=$(curl "https://github.com/$owner?tab=packages&visibility=public&&per_page=100&page=$packages_page")
        fi

        packages_lines=$(grep -zoP 'href="/'"$owner_type"'/'"$owner"'/packages/[^/]+/package/[^"]+"' <<<"$html" | tr -d '\0')
        [ -n "$packages_lines" ] || break
        packages_lines=${packages_lines//href=/\\nhref=}
        packages_lines=${packages_lines//\\n/$'\n'} # replace \n with newline

        # loop through the packages in $packages_lines
        while IFS= read -r line; do
            [ -n "$line" ] || continue
            package_new=$(cut -d'/' -f7 <<<"$line" | tr -d '"')
            package_type=$(cut -d'/' -f5 <<<"$line")
            repo=$(grep -zoP '(?<=href="/'"$owner_type"'/'"$owner"'/packages/'"$package_type"'/package/'"$package_new"'")(.|\n)*?href="/'"$owner"'/[^"]+"' <<<"$html" | tr -d '\0' | grep -oP 'href="/'"$owner"'/[^"]+' | cut -d'/' -f3)
            [ -n "$packages" ] && packages="$packages"$'\n'"$package_type/$repo/$package_new" || packages="$package_type/$repo/$package_new"
        done <<<"$packages_lines"
    done

    # deduplicate and array-ify the packages
    packages=$(awk '!seen[$0]++' <<<"$packages")
    readarray -t packages <<<"$packages"
    #printf "%s\n" "${packages[@]}" | env_parallel -j 1000% --bar -X update_packages >/dev/null
    run_parallel update_package "$(printf "%s\n" "${packages[@]}")"
    echo "Processed $owner"
}

refresh_owner() {
    [ -d "$BKG_INDEX_DIR" ] || mkdir "$BKG_INDEX_DIR"
    owner=$1
    [ -n "$owner" ] || return
    echo "Processing $owner..."
    # create the owner's json file
    echo "[" >"$BKG_INDEX_DIR"/"$owner".json

    # go through each package in the index
    sqlite3 "$BKG_INDEX_DB" "select * from '$BKG_INDEX_TBL_PKG' where owner='$owner' order by downloads + 0 asc;" | while IFS='|' read -r owner_id owner_type package_type _ repo package downloads downloads_month downloads_week downloads_day size date; do
        script_now=$(date -u +%s)
        script_diff=$((script_now - SCRIPT_START))

        if ((script_diff >= 21500)); then
            echo "Script has been running for 6 hours. Committing changes..."
            break
        fi
        echo "Refreshing $owner/$package..."

        # only use the latest date for the package
        query="select date from '$BKG_INDEX_TBL_PKG' where owner_id='$owner_id' and package='$package' order by date desc limit 1;"
        max_date=$(sqlite3 "$BKG_INDEX_DB" "$query")
        [ "$date" = "$max_date" ] || continue

        fmt_downloads=$(numfmt <<<"$downloads")
        version_count=0
        version_with_tag_count=0
        table_version_name="${BKG_INDEX_TBL_VER}_${owner_type}_${package_type}_${owner}_${repo}_${package}"

        # get the version and tagged counts
        query="select name from sqlite_master where type='table' and name='$table_version_name';"
        table_exists=$(sqlite3 "$BKG_INDEX_DB" "$query")

        if [ -n "$table_exists" ]; then
            query="select count(distinct id) from '$table_version_name';"
            version_count=$(sqlite3 "$BKG_INDEX_DB" "$query")
            query="select count(distinct id) from '$table_version_name' where tags != '' and tags is not null;"
            version_with_tag_count=$(sqlite3 "$BKG_INDEX_DB" "$query")
        fi

        echo "{" >>"$BKG_INDEX_DIR"/"$owner".json
        [[ "$package_type" != "container" ]] || echo "\"image\": \"$package\",\"pulls\": \"$fmt_downloads\"," >>"$BKG_INDEX_DIR"/"$owner".json
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
            \"raw_size\": $size,
            \"raw_versions\": $version_count,
            \"raw_tagged\": $version_with_tag_count,
            \"raw_downloads\": $downloads,
            \"raw_downloads_month\": $downloads_month,
            \"raw_downloads_week\": $downloads_week,
            \"raw_downloads_day\": $downloads_day,
            \"version\": [" >>"$BKG_INDEX_DIR"/"$owner".json

        # add the versions to index/"$owner".json
        if [ "$version_count" -gt 0 ]; then
            query="select id from '$table_version_name' order by id desc limit 1;"
            version_newest_id=$(sqlite3 "$BKG_INDEX_DB" "$query")

            # get only the last day each version was updated, which may not be today
            # desc sort by id
            query="select id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags from '$table_version_name' group by id order by id desc;"
            sqlite3 "$BKG_INDEX_DB" "$query" | while IFS='|' read -r vid vname vsize vdownloads vdownloads_month vdownloads_week vdownloads_day vdate vtags; do
                echo "{
                    \"id\": $vid,
                    \"name\": \"$vname\",
                    \"date\": \"$vdate\",
                    \"newest\": $([ "$vid" = "$version_newest_id" ] && echo "true" || echo "false"),
                    \"size\": \"$(numfmt_size <<<"$vsize")\",
                    \"downloads\": \"$(numfmt <<<"$vdownloads")\",
                    \"downloads_month\": \"$(numfmt <<<"$vdownloads_month")\",
                    \"downloads_week\": \"$(numfmt <<<"$vdownloads_week")\",
                    \"downloads_day\": \"$(numfmt <<<"$vdownloads_day")\",
                    \"raw_size\": $vsize,
                    \"raw_downloads\": $vdownloads,
                    \"raw_downloads_month\": $vdownloads_month,
                    \"raw_downloads_week\": $vdownloads_week,
                    \"raw_downloads_day\": $vdownloads_day,
                    \"tags\": [\"${vtags//,/\",\"}\"]
                    }," >>"$BKG_INDEX_DIR"/"$owner".json
            done
        fi

        # remove the last comma
        sed -i '$ s/,$//' "$BKG_INDEX_DIR"/"$owner".json
        echo "]
        }," >>"$BKG_INDEX_DIR"/"$owner".json
        echo
    done

    # remove the last comma
    sed -i '$ s/,$//' "$BKG_INDEX_DIR"/"$owner".json
    echo "]" >>"$BKG_INDEX_DIR"/"$owner".json

    # if the json is empty, exit
    jq -e 'length > 0' "$BKG_INDEX_DIR"/"$owner".json || return

    # sort the top level by raw_downloads
    jq -c 'sort_by(.raw_downloads | tonumber) | reverse' "$BKG_INDEX_DIR"/"$owner".json >"$BKG_INDEX_DIR"/"$owner".tmp.json
    mv "$BKG_INDEX_DIR"/"$owner".tmp.json "$BKG_INDEX_DIR"/"$owner".json

    # if the json is over 100MB, remove oldest versions from the packages with the most versions
    json_size=$(stat -c %s "$BKG_INDEX_DIR"/"$owner".json)
    while [ "$json_size" -ge 100000000 ]; do
        jq -e 'map(.version | length > 0) | any' "$BKG_INDEX_DIR"/"$owner".json || break
        jq -c 'sort_by(.versions | tonumber) | reverse | map(select(.versions > 0)) | map(.version |= sort_by(.id | tonumber) | del(.version[0]))' "$BKG_INDEX_DIR"/"$owner".json >"$BKG_INDEX_DIR"/"$owner".tmp.json
        mv "$BKG_INDEX_DIR"/"$owner".tmp.json "$BKG_INDEX_DIR"/"$owner".json
        json_size=$(stat -c %s "$BKG_INDEX_DIR"/"$owner".json)
    done
    echo "Processed $owner"
}

if [ ! -f "$BKG_INDEX_DB" ]; then
    command curl -sSLNZO "https://github.com/$GITHUB_OWNER/$GITHUB_REPO/releases/latest/download/$BKG_INDEX_SQL.zst"
    zstd -d "$BKG_INDEX_SQL.zst" | sqlite3 "$BKG_INDEX_DB"
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
