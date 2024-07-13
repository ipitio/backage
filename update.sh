#!/bin/bash
# Scrape each package
# Usage: ./update.sh
# Dependencies: curl, jq, sqlite3, docker
# Copyright (c) ipitio
#
# shellcheck disable=SC1091,SC2015

source lib.sh

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
    [ -f "$(get_BKG BKG_INDEX_DB)" ] || return 1
    rotated=false
    echo "Compressing the database..."
    sqlite3 "$(get_BKG BKG_INDEX_DB)" ".dump" | zstd -22 --ultra --long -T0 -o "$(get_BKG BKG_INDEX_SQL)".zst.new

    if [ -f "$(get_BKG BKG_INDEX_SQL)".zst.new ]; then
        # rotate the database if it's greater than 2GB
        if [ "$(stat -c %s "$(get_BKG BKG_INDEX_SQL)".zst.new)" -ge 2000000000 ]; then
            rotated=true
            echo "Rotating the database..."
            [ -d "$(get_BKG BKG_INDEX_SQL)".d ] || mkdir "$(get_BKG BKG_INDEX_SQL)".d
            [ ! -f "$(get_BKG BKG_INDEX_SQL)".zst ] || mv "$(get_BKG BKG_INDEX_SQL)".zst "$(get_BKG BKG_INDEX_SQL)".d/"$(date -u +%Y.%m.%d)".zst
            query="delete from '$(get_BKG BKG_INDEX_TBL_PKG)' where date not between date('$(get_BKG BKG_BATCH_FIRST_STARTED)') and date('$TODAY');"
            sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query"
            query="select name from sqlite_master where type='table' and name like '$(get_BKG BKG_INDEX_TBL_VER)_%';"
            tables=$(sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query")

            for table in $tables; do
                query="delete from '$table' where date not between date('$(get_BKG BKG_BATCH_FIRST_STARTED)') and date('$TODAY');"
                sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query"
            done

            sqlite3 "$(get_BKG BKG_INDEX_DB)" "vacuum;"
            sqlite3 "$(get_BKG BKG_INDEX_DB)" ".dump" | zstd -22 --ultra --long -T0 -o "$(get_BKG BKG_INDEX_SQL)".zst.new
        fi

        mv "$(get_BKG BKG_INDEX_SQL)".zst.new "$(get_BKG BKG_INDEX_SQL)".zst
        echo "Compressed the database"
    else
        echo "Failed to compress the database!"
    fi

    # if the database is smaller than 1kb, return 1
    [ "$(stat -c %s "$(get_BKG BKG_INDEX_SQL)".zst)" -ge 1000 ] || return 1
    echo "Updating the CHANGELOG..."
    [ ! -f CHANGELOG.md ] || rm -f CHANGELOG.md
    \cp templates/.CHANGELOG.md CHANGELOG.md
    query="select count(distinct owner_id) from '$(get_BKG BKG_INDEX_TBL_PKG)';"
    owners=$(sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query")
    query="select count(distinct repo) from '$(get_BKG BKG_INDEX_TBL_PKG)';"
    repos=$(sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query")
    query="select count(distinct package) from '$(get_BKG BKG_INDEX_TBL_PKG)';"
    packages=$(sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query")
    perl -0777 -pe 's/\[OWNERS\]/'"$owners"'/g; s/\[REPOS\]/'"$repos"'/g; s/\[PACKAGES\]/'"$packages"'/g' CHANGELOG.md >CHANGELOG.tmp && [ -f CHANGELOG.tmp ] && mv CHANGELOG.tmp CHANGELOG.md || :
    ! $rotated || echo " The database grew over 2GB and was rotated, but you can find all previous data under [Releases](https://github.com/$GITHUB_OWNER/$GITHUB_REPO/releases)." >>CHANGELOG.md
    echo "Updated the CHANGELOG"
    # if index db is greater than 100MB, remove it
    if [ "$(stat -c %s "$(get_BKG BKG_INDEX_DB)")" -ge 100000000 ]; then
        echo "Removing the database..."
        rm -f "$(get_BKG BKG_INDEX_DB)"
        [ ! -f index.json ] || rm -f index.json
        echo "Removed the database"
    fi
}

# shellcheck disable=SC2317
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
    table_version_name="$(get_BKG BKG_INDEX_TBL_VER)_${owner_type}_${package_type}_${owner}_${repo}_${package}"
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
    sqlite3 "$(get_BKG BKG_INDEX_DB)" "$table_version"
    search="select count(*) from '$table_version_name' where id='$version_id' and date between date('$(get_BKG BKG_BATCH_FIRST_STARTED)') and date('$TODAY');"
    count=$(sqlite3 "$(get_BKG BKG_INDEX_DB)" "$search")

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
        sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query"
    fi
    echo "Finished $owner/$package/$version_id"
}

# shellcheck disable=SC2317
update_package() {
    check_limit || return
    packages_line=$1
    [ -n "$packages_line" ] || return
    package_type=$(cut -d'/' -f1 <<<"$packages_line")
    repo=$(cut -d'/' -f2 <<<"$packages_line")
    package=$(cut -d'/' -f3 <<<"$packages_line")

    # optout.txt has lines like "owner/repo/package"
    if [ -f optout.txt ]; then
        while IFS= read -r line; do
            [ -n "$line" ] || continue

            if [ "$line" = "$owner/$repo/$package" ]; then
                # remove the package from the db
                query="delete from '$(get_BKG BKG_INDEX_TBL_PKG)' where owner_id='$owner_id' and package='$package';"
                sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query"
                table_version_name="$(get_BKG BKG_INDEX_TBL_VER)_${owner_type}_${package_type}_${owner}_${repo}_${package}"
                query="drop table if exists '$table_version_name';"
                sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query"
                return
            fi
        done <optout.txt
    fi

    # manual update: skip if the package is already in the index; the rest are updated daily
    if [ "$1" = "1" ] && [[ "$owner" != "arevindh" ]]; then
        query="select count(*) from '$(get_BKG BKG_INDEX_TBL_PKG)' where owner_id='$owner_id' and package='$package';"
        count=$(sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query")
        [[ "$count" =~ ^0*$ ]] || return
    fi

    # update stats
    query="select count(*) from '$(get_BKG BKG_INDEX_TBL_PKG)' where owner_id='$owner_id' and package='$package' and date between date('$(get_BKG BKG_BATCH_FIRST_STARTED)') and date('$TODAY');"
    count=$(sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query")
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
        table_version_name="$(get_BKG BKG_INDEX_TBL_VER)_${owner_type}_${package_type}_${owner}_${repo}_${package}"
        query="select name from sqlite_master where type='table' and name='$table_version_name';"
        table_exists=$(sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query")

        if [ -n "$table_exists" ]; then
            query="select id, name, tags from '$table_version_name';"

            while IFS='|' read -r id name tags; do
                if ! jq -e ".[] | select(.id == \"$id\")" <<<"$versions_json" &>/dev/null; then
                    versions_json=$(jq ". += [{\"id\":\"$id\",\"name\":\"$name\",\"tags\":\"$tags\"}]" <<<"$versions_json")
                fi
            done < <(sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query")
        fi

        versions_per_page=$(get_BKG BKG_VERSIONS_PER_PAGE)
        while true; do
            check_limit || return
            ((versions_page++))
            # if the repo is public the api request should succeed
            versions_json_more="[]"

            if [ -n "$GITHUB_TOKEN" ]; then
                versions_json_more=$(curl -H "Accept: application/vnd.github+json" \
                    -H "Authorization: Bearer $GITHUB_TOKEN" \
                    -H "X-GitHub-Api-Version: 2022-11-28" \
                    "https://api.github.com/$owner_type/$owner/packages/$package_type/$package/versions?per_page=$versions_per_page&page=$versions_page")
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
            table_exists=$(sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query")

            if [ -n "$table_exists" ]; then
                # calculate the total downloads
                query="select max(date) from '$table_version_name';"
                max_date=$(sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query")
                query="select sum(downloads), sum(downloads_month), sum(downloads_week), sum(downloads_day) from '$table_version_name' where date='$max_date';"
                # summed_raw_downloads=$(sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query" | cut -d'|' -f1)
                raw_downloads_month=$(sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query" | cut -d'|' -f2)
                raw_downloads_week=$(sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query" | cut -d'|' -f3)
                raw_downloads_day=$(sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query" | cut -d'|' -f4)

                # use the latest version's size as the package size
                query="select id from '$(get_BKG BKG_INDEX_TBL_VER)_${owner_type}_${package_type}_${owner}_${repo}_${package}' order by id desc limit 1;"
                version_newest_id=$(sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query")
                query="select size from '$(get_BKG BKG_INDEX_TBL_VER)_${owner_type}_${package_type}_${owner}_${repo}_${package}' where id='$version_newest_id' order by date desc limit 1;"
                size=$(sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query")
            fi

            query="insert or replace into '$(get_BKG BKG_INDEX_TBL_PKG)' (owner_id, owner_type, package_type, owner, repo, package, downloads, downloads_month, downloads_week, downloads_day, size, date) values ('$owner_id', '$owner_type', '$package_type', '$owner', '$repo', '$package', '$raw_downloads', '$raw_downloads_month', '$raw_downloads_week', '$raw_downloads_day', '$size', '$TODAY');"
            sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query"
        fi
    fi
}

# shellcheck disable=SC2317
update_owners() {
    for login_id in $1; do
        check_limit || return
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
    done
}

main() {
    # remove owners from owners.txt that have already been scraped in this batch
    [ -n "$(get_BKG BKG_BATCH_FIRST_STARTED)" ] || set_BKG BKG_BATCH_FIRST_STARTED "$TODAY"

    if [ -s owners.txt ] && [ "$1" = "0" ]; then
        owners_to_remove=()

        while IFS= read -r owner; do
            check_limit || return
            [ -n "$owner" ] || continue
            [[ "$owner" =~ .*\/.* ]] && owner_id=$(cut -d'/' -f1 <<<"$owner") || owner_id=""

            if [ -z "$owner_id" ]; then
                query="select count(*) from '$(get_BKG BKG_INDEX_TBL_PKG)' where owner='$owner' and date between date('$(get_BKG BKG_BATCH_FIRST_STARTED)') and date('$TODAY');"
            else
                query="select count(*) from '$(get_BKG BKG_INDEX_TBL_PKG)' where owner_id='$owner_id' and date between date('$(get_BKG BKG_BATCH_FIRST_STARTED)') and date('$TODAY');"
            fi

            count=$(sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query")
            [[ "$count" =~ ^0*$ ]] || owners_to_remove+=("$owner")
        done <owners.txt

        for owner_to_remove in "${owners_to_remove[@]}"; do
            sed -i "/$owner_to_remove/d" owners.txt
        done
    fi

    [ -s owners.txt ] || set_BKG BKG_BATCH_FIRST_STARTED "$TODAY"
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

    # if this is a scheduled update, scrape all owners that haven't been scraped in this batch
    if [ "$1" = "0" ]; then
        # get more owners if no more
        if [ ! -s owners.txt ]; then
            # get new owners
            echo "Finding more owners..."
            owners_page=0
            [ -n "$(get_BKG BKG_LAST_SCANNED_ID)" ] || set_BKG BKG_LAST_SCANNED_ID "0"
            last_scanned_id=$(get_BKG BKG_LAST_SCANNED_ID)
            while [ "$owners_page" -lt 10 ]; do
                check_limit || return
                ((owners_page++))
                owners_more="[]"

                if [ -n "$GITHUB_TOKEN" ]; then
                    owners_more=$(curl -H "Accept: application/vnd.github+json" \
                        -H "Authorization: Bearer $GITHUB_TOKEN" \
                        -H "X-GitHub-Api-Version: 2022-11-28" \
                        "https://api.github.com/users?per_page=100&page=$owners_page&since=$last_scanned_id")
                    # increment BKG_CALLS_TO_API in env.env
                    calls_to_api=$(get_BKG BKG_CALLS_TO_API)
                    min_calls_to_api=$(get_BKG BKG_MIN_CALLS_TO_API)
                    ((calls_to_api++))
                    ((min_calls_to_api++))
                    set_BKG BKG_CALLS_TO_API "$calls_to_api"
                    set_BKG BKG_MIN_CALLS_TO_API "$min_calls_to_api"
                    jq -e . <<<"$owners_more" &>/dev/null || owners_more="[]"
                fi

                # if owners doesn't have .login, break
                jq -e '.[].login' <<<"$owners_more" &>/dev/null || break

                # add the new owners to the owners array
                for i in $(jq -r '.[] | @base64' <<<"$owners_more"); do
                    _jq() {
                        echo "$i" | base64 --decode | jq -r "$@"
                    }

                    owner=$(_jq '.login')
                    id=$(_jq '.id')
                    [ -n "$owner" ] || continue
                    grep -q "$owner" owners.txt || echo "$id/$owner" >>owners.txt
                done
            done
        fi

        # add the owners in the database to the owners array
        echo "Reading known owners..."
        query="select owner_id, owner from '$(get_BKG BKG_INDEX_TBL_PKG)' where date not between date('$(get_BKG BKG_BATCH_FIRST_STARTED)') and date('$TODAY') group by owner_id;"

        while IFS= read -r owner_id owner; do
            check_limit || return
            [ -n "$owner" ] || continue
            grep -q "$owner" owners.txt || echo "$owner_id/$owner" >>owners.txt
        done < <(sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query")
    fi

    owners=()

    # add more owners
    if [ -s owners.txt ]; then
        echo "Queuing owners..."
        sed -i '/^\s*$/d' owners.txt
        echo >>owners.txt
        awk 'NF' owners.txt >owners.tmp && mv owners.tmp owners.txt
        sed -i 's/^[[:space:]]*//;s/[[:space:]]*$//' owners.txt

        while IFS= read -r owner; do
            check_limit || return
            owner=$(echo "$owner" | tr -d '[:space:]')
            [ -n "$owner" ] || continue
            owner_id=""

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

            grep -q "$owner_id/$owner" <<<"${owners[*]}" || owners+=("$owner_id/$owner")
        done <owners.txt
    fi

    # scrape the owners
    echo "Forking jobs..."
    printf "%s\n" "${owners[@]}" | env_parallel -j 1000% --lb -X update_owners
    #update_owners "${owners[@]}"
    echo "Completed jobs"
    xz_db
    return $?
}

main "$@"
exit $?
