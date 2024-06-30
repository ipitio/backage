#!/bin/bash
# Scrape each package
# Usage: ./update.sh
# Dependencies: curl, jq, sqlite3, Docker
# Copyright (c) ipitio
#
# shellcheck disable=SC2015

# check if curl and jq are installed
if ! command -v curl &>/dev/null || ! command -v jq &>/dev/null || ! command -v sqlite3 &>/dev/null; then
    sudo apt-get update
    sudo apt-get install curl jq sqlite3 -y
fi

declare SCRIPT_START
declare TODAY
SCRIPT_START=$(date +%s)
TODAY=$(date -u +%Y-%m-%d)
readonly SCRIPT_START TODAY
declare -r INDEX_DB="index.db" # sqlite
declare -r table_pkg_name="packages"
rate_limit_start=$(date +%s)
calls_to_api=0
source lib.sh

check_limit() {
    rate_limit_end=$(date +%s)
    rate_limit_diff=$((rate_limit_end - rate_limit_start))
    hours_passed=$((rate_limit_diff / 3600))
    script_limit_diff=$((rate_limit_end - SCRIPT_START))

    # if the script has been running for more than 5 hours, exit
    if ((script_limit_diff >= 18000)); then
        echo "Script has been running for more than 5 hours. Saving..."
        return 1
    fi

    # adjust the limit based on the number of hours passed
    if ((calls_to_api >= 1000 * (hours_passed + 1))); then
        echo "$calls_to_api calls to the GitHub API in $((rate_limit_diff / 60)) minutes"
        remaining_time=$((3600 * (hours_passed + 1) - rate_limit_diff))
        echo "Sleeping for $remaining_time seconds..."
        sleep $remaining_time
        echo "Resuming..."
        rate_limit_start=$(date +%s)
        calls_to_api=0
    fi

    return 0
}

# use a sqlite database to store the downloads of a package
[ -f "$INDEX_DB" ] || touch "$INDEX_DB"
table_pkg="create table if not exists '$table_pkg_name' (
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
);"
sqlite3 "$INDEX_DB" "$table_pkg"

# we will incrementally add new owners to the index
# file "id" contains the highest id of the last owner
since=-1
[ ! -f id ] || since=$(<id)

if [ "$1" = "0" ]; then
    # get new owners
    while [ "$owners_page" -lt 3 ]; do
        check_limit || break
        ((owners_page++))
        owners_more="[]"

        if [ -n "$GITHUB_TOKEN" ]; then
            owners_more=$(curl -sSL \
                -H "Accept: application/vnd.github+json" \
                -H "Authorization: Bearer $GITHUB_TOKEN" \
                -H "X-GitHub-Api-Version: 2022-11-28" \
                --connect-timeout 60 -m 120 \
                "https://api.github.com/users?per_page=100&page=$owners_page&since=$since")
            ((calls_to_api++))
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

            if ! grep -q "$owner" owners.txt; then
                echo "$owner_id/$owner" >>owners.txt
            fi

            echo "$id" >id
        done
    done

    # add the owners in the database to the owners array
    query="select owner_id, owner from '$table_pkg_name';"
    while IFS='|' read -r owner_id owner; do
        # if owner_id is null, find the owner_id
        if [ -z "$owner_id" ]; then
            owner_id=$(curl -sSL \
                -H "Accept: application/vnd.github+json" \
                -H "Authorization: Bearer $GITHUB_TOKEN" \
                -H "X-GitHub-Api-Version: 2022-11-28" \
                --connect-timeout 60 -m 120 \
                "https://api.github.com/users/$owner" | jq -r '.id')
            ((calls_to_api++))
            query="update '$table_pkg_name' set owner_id='$owner_id' where owner='$owner';"
            sqlite3 "$INDEX_DB" "$query"
        fi

        if ! grep -q "$owner_id/$owner" <<<"${owners[*]}"; then
            owners+=("$owner_id/$owner")
        fi
    done < <(sqlite3 "$INDEX_DB" "$query")
fi

# if owners.txt exists, read any owners from there
if [ -f owners.txt ]; then
    sed -i '/^\s*$/d' owners.txt
    echo >>owners.txt
    awk 'NF' owners.txt >owners.tmp && mv owners.tmp owners.txt
    sed -i 's/^[[:space:]]*//;s/[[:space:]]*$//' owners.txt

    while IFS= read -r owner; do
        check_limit || break
        owner=$(echo "$owner" | tr -d '[:space:]')
        [ -n "$owner" ] || continue
        if [[ "$owner" =~ *\/* ]]; then
            owner_id=$(cut -d'/' -f1 <<<"$owner")
            owner=$(cut -d'/' -f2 <<<"$owner")
        else
            owner_id=$(curl -sSL \
                -H "Accept: application/vnd.github+json" \
                -H "Authorization: Bearer $GITHUB_TOKEN" \
                -H "X-GitHub-Api-Version: 2022-11-28" \
                --connect-timeout 60 -m 120 \
                "https://api.github.com/users/$owner" | jq -r '.id')
            ((calls_to_api++))
        fi

        if ! grep -q "$owner_id/$owner" <<<"${owners[*]}"; then
            owners+=("$owner_id/$owner")
        fi
    done <owners.txt
fi

# loop through known and new owners
for id_login in "${owners[@]}"; do
    check_limit || break
    owner=$(cut -d'/' -f2 <<<"$id_login")
    owner_id=$(cut -d'/' -f1 <<<"$id_login")
    owner_type="orgs"
    html=$(curl "https://github.com/orgs/$owner/people")
    is_org=$(grep -zoP 'href="/orgs/'"$owner"'/people"' <<<"$html" | tr -d '\0')
    [ -n "$is_org" ] || owner_type="users"
    packages=""
    packages_page=0
    echo "Getting packages for $owner_type/$owner..."

    # get the packages
    while true; do
        check_limit || break
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

    if [ "${#packages[@]}" -gt 0 ] && [ -n "${packages[0]}" ]; then
        printf "Got packages: "
        for i in "${packages[@]}"; do
            printf "%s " "$i"
        done
        echo
    fi

    # loop through the packages in $packages
    for package_line in "${packages[@]}"; do
        check_limit || break
        [ -n "$package_line" ] || continue
        package_type=$(cut -d'/' -f1 <<<"$package_line")
        repo=$(cut -d'/' -f2 <<<"$package_line")
        package=$(cut -d'/' -f3 <<<"$package_line")

        # optout.txt has lines like "owner/repo/package"
        if [ -f optout.txt ]; then
            while IFS= read -r line; do
                [ -n "$line" ] || continue
                if [ "$line" = "$owner/$repo/$package" ]; then
                    # remove the package from the db
                    query="delete from '$table_pkg_name' where owner_type='$owner_type' and package_type='$package_type' and owner='$owner' and repo='$repo' and package='$package';"
                    sqlite3 "$INDEX_DB" "$query"
                    table_version_name="versions_${owner_type}_${package_type}_${owner}_${repo}_${package}"
                    query="drop table if exists '$table_version_name';"
                    sqlite3 "$INDEX_DB" "$query"
                    continue 2
                fi
            done <optout.txt
        fi

        # manual update: skip if the package is already in the index; the rest are updated daily
        if [ "$1" = "1" ]; then
            query="select count(*) from '$table_pkg_name' where owner_type='$owner_type' and package_type='$package_type' and owner='$owner' and repo='$repo' and package='$package';"
            count=$(sqlite3 "$INDEX_DB" "$query")
            [[ "$count" =~ ^0$ ]] || continue
        fi

        # update stats
        query="select count(*) from '$table_pkg_name' where owner_type='$owner_type' and package_type='$package_type' and owner='$owner' and repo='$repo' and package='$package' and date='$TODAY';"
        count=$(sqlite3 "$INDEX_DB" "$query")

        if [[ "$count" =~ ^0$ ]]; then
            downloads=-1
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
            [ -n "$is_public" ] || continue
            echo "Scraping $package_type/$repo/$package..."
            raw_downloads=$(grep -Pzo 'Total downloads[^"]*"\d*' <<<"$html" | grep -Pzo '\d*$' | tr -d '\0') # https://stackoverflow.com/a/74214537
            [[ ! "$raw_downloads" =~ ^[0-9]+$ ]] || downloads=$(numfmt <<<"$raw_downloads")
            versions_json="[]"
            versions_page=0

            # add all the versions currently in the db to the versions_json, if they are not already there
            table_version_name="versions_${owner_type}_${package_type}_${owner}_${repo}_${package}"
            query="select name from sqlite_master where type='table' and name='$table_version_name';"
            table_exists=$(sqlite3 "$INDEX_DB" "$query")

            if [ -n "$table_exists" ]; then
                query="select id, name from '$table_version_name';"
                while IFS='|' read -r id name; do
                    if ! jq -e ".[] | select(.id == \"$id\")" <<<"$versions_json" &>/dev/null; then
                        versions_json=$(jq ". += [{\"id\":\"$id\",\"name\":\"$name\"}]" <<<"$versions_json")
                    fi
                done < <(sqlite3 "$INDEX_DB" "$query")
            fi

            # limit to X pages / newest X00 versions
            while [ "$versions_page" -lt 1 ]; do
                check_limit || break
                ((versions_page++))
                # if the repo is public the api request should succeed
                versions_json_more="[]"

                if [ -n "$GITHUB_TOKEN" ]; then
                    versions_json_more=$(curl -sSL \
                        -H "Accept: application/vnd.github+json" \
                        -H "Authorization: Bearer $GITHUB_TOKEN" \
                        -H "X-GitHub-Api-Version: 2022-11-28" \
                        --connect-timeout 60 -m 120 \
                        "https://api.github.com/$owner_type/$owner/packages/$package_type/$package/versions?per_page=100&page=$versions_page")
                    ((calls_to_api++))
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

                    if ! jq -e ".[] | select(.id == \"$id\")" <<<"$versions_json" &>/dev/null; then
                        versions_json=$(jq ". += [{\"id\":\"$id\",\"name\":\"$name\"}]" <<<"$versions_json")
                    fi
                done
            done

            # scan the versions
            jq -e . <<<"$versions_json" &>/dev/null || versions_json="[{\"id\":\"latest\",\"name\":\"latest\"}]"
            for i in $(jq -r '.[] | @base64' <<<"$versions_json"); do
                _jq() {
                    echo "$i" | base64 --decode | jq -r "$@"
                }

                check_limit || break
                version_size=-1
                version_id=$(_jq '.id')
                version_name=$(_jq '.name')
                version_tags=""
                table_version_name="versions_${owner_type}_${package_type}_${owner}_${repo}_${package}"
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
                sqlite3 "$INDEX_DB" "$table_version"
                search="select count(*) from '$table_version_name' where id='$version_id' and date='$TODAY';"
                count=$(sqlite3 "$INDEX_DB" "$search")

                # insert a new row
                if [[ "$count" =~ ^0$ ]]; then
                    if [ "$package_type" = "container" ]; then
                        # get the size by adding up the layers
                        [[ "$version_name" =~ ^sha256:.+$ ]] && sep="@" || sep=":"
                        manifest=$(docker manifest inspect -v "ghcr.io/$lower_owner/$lower_package$sep$version_name")
                        [[ ! "$manifest" =~ ^\[.*\]$ ]] || manifest=$(jq '.[]' <<<"$manifest")

                        if [[ "$manifest" =~ ^\{.*\}$ ]]; then
                            if [[ -n "$(jq '.. | try .layers[]' <<<"$manifest")" ]]; then
                                version_size=$(jq '.. | try .size | select(. > 0)' <<<"$manifest" | awk '{s+=$1} END {print s}')
                                [[ "$version_size" =~ ^[0-9]+$ ]] || version_size=-1
                            elif [[ -n "$(jq '.. | try .manifests[]' <<<"$manifest")" ]]; then
                                version_size=$(jq '.. | try .size | select(. > 0)' <<<"$manifest" | awk '{s+=$1} END {print s/NR}')
                                [[ "$version_size" =~ ^[0-9]+$ ]] || version_size=-1
                            fi

                            for tag in $(_jq '.. | try .tags[]'); do
                                [ -z "$tag" ] || version_tags="$version_tags$tag,"
                            done
                        fi

                        # remove the last comma
                        version_tags=${version_tags%,}
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

                    query="insert into '$table_version_name' (id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('$version_id', '$version_name', '$version_size', '$version_raw_downloads', '$version_raw_downloads_month', '$version_raw_downloads_week', '$version_raw_downloads_day', '$TODAY', '$version_tags');"
                    sqlite3 "$INDEX_DB" "$query"
                #else query="update '$table_version_name' set size='$version_size', downloads='$version_raw_downloads', downloads_month='$version_raw_downloads_month', downloads_week='$version_raw_downloads_week', downloads_day='$version_raw_downloads_day', tags='$version_tags' where id='$version_id' and date='$TODAY';"
                fi
            done

            # use the version stats if we have them
            query="select name from sqlite_master where type='table' and name='$table_version_name';"
            table_exists=$(sqlite3 "$INDEX_DB" "$query")

            if [ -n "$table_exists" ]; then
                # calculate the total downloads over all versions for day, week, and month using sqlite:
                query="select sum(downloads), sum(downloads_month), sum(downloads_week), sum(downloads_day) from 'versions_${owner_type}_${package_type}_${owner}_${repo}_${package}' where date='$TODAY';"
                # summed_raw_downloads=$(sqlite3 "$INDEX_DB" "$query" | cut -d'|' -f1)
                raw_downloads_month=$(sqlite3 "$INDEX_DB" "$query" | cut -d'|' -f2)
                raw_downloads_week=$(sqlite3 "$INDEX_DB" "$query" | cut -d'|' -f3)
                raw_downloads_day=$(sqlite3 "$INDEX_DB" "$query" | cut -d'|' -f4)

                # use the latest version's size as the package size
                query="select id from 'versions_${owner_type}_${package_type}_${owner}_${repo}_${package}' order by id desc limit 1;"
                version_newest_id=$(sqlite3 "$INDEX_DB" "$query")
                query="select size from 'versions_${owner_type}_${package_type}_${owner}_${repo}_${package}' where id='$version_newest_id' order by date desc limit 1;"
                size=$(sqlite3 "$INDEX_DB" "$query")
            fi

            query="insert into '$table_pkg_name' (owner_id, owner_type, package_type, owner, repo, package, downloads, downloads_month, downloads_week, downloads_day, size, date) values ('$owner_id', '$owner_type', '$package_type', '$owner', '$repo', '$package', '$raw_downloads', '$raw_downloads_month', '$raw_downloads_week', '$raw_downloads_day', '$size', '$TODAY');"
            sqlite3 "$INDEX_DB" "$query"
        #else query="update '$table_pkg_name' set owner_id='$owner_id', downloads='$raw_downloads', downloads_month='$raw_downloads_month', downloads_week='$raw_downloads_week', downloads_day='$raw_downloads_day', size='$size' where owner_type='$owner_type' and package_type='$package_type' and owner='$owner' and repo='$repo' and package='$package' and date='$TODAY';"
        fi
    done
    [ "$owner" = "arevindh" ] || sed -i "/$owner/d" owners.txt
done
