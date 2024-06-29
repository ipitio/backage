#!/bin/bash
# Update the stats for each package
# Usage: ./update.sh
# Dependencies: curl, jq, sqlite3, Docker
# Copyright (c) ipitio
#
# shellcheck disable=SC2015

declare -r INDEX_DB="index.db" # sqlite
declare TODAY
TODAY=$(date -u +%Y-%m-%d)
readonly TODAY

# check if curl and jq are installed
if ! command -v curl &>/dev/null || ! command -v jq &>/dev/null || ! command -v sqlite3 &>/dev/null; then
    sudo apt-get update
    sudo apt-get install curl jq sqlite3 -y
fi

# use a sqlite database to store the downloads of a package
[ -f "$INDEX_DB" ] || touch "$INDEX_DB"
table_pkg_name="packages"
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
    primary key (owner_type, package_type, owner, repo, package)
);"
sqlite3 "$INDEX_DB" "$table_pkg"

# we will incrementally add new owners to the index
# file "id" contains the highest id of the last owner
since=-1
[ ! -f id ] || since=$(<id)
owners=()
owners_page=0
rate_limit_start=$(date +%s)
calls_to_api=0

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

check_limit() {
    rate_limit_end=$(date +%s)
    rate_limit_diff=$((rate_limit_end - rate_limit_start))
    hours_passed=$((rate_limit_diff / 3600))

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
}

# if owners.txt exists, read any owners from there
if [ -f owners.txt ]; then
    [ "$(tail -c 1 owners.txt)" = $'\n' ] || echo >>owners.txt
    while IFS= read -r owner; do
        owner=$(echo "$owner" | tr -d '[:space:]')
        [ -n "$owner" ] || continue
        owner_id=$(curl -sSL \
            -H "Accept: application/vnd.github+json" \
            -H "Authorization: Bearer $GITHUB_TOKEN" \
            -H "X-GitHub-Api-Version: 2022-11-28" \
            --connect-timeout 60 -m 120 \
            "https://api.github.com/users/$owner" | jq -r '.id')
        ((calls_to_api++))

        if ! grep -q "$owner_id/$owner" <<<"${owners[*]}"; then
            owners+=("$owner_id/$owner")
        fi

        check_limit
    done <owners.txt

    [ "${owners[0]}" = "693151/arevindh" ] || : >owners.txt
fi

if [ "$1" = "0" ]; then
    # get new owners
    while [ "$owners_page" -lt 10 ]; do
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

            if ! grep -q "$owner_id/$owner" <<<"${owners[*]}"; then
                owners+=("$owner_id/$owner")
            fi

            echo "$id" >id
        done

        check_limit
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

        check_limit
    done < <(sqlite3 "$INDEX_DB" "$query")
fi

# loop through known and new owners
for id_login in "${owners[@]}"; do
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
        ((packages_page++))

        if [ "$owner_type" = "orgs" ]; then
            html=$(curl "https://github.com/$owner_type/$owner/packages?visibility=public&per_page=100&page=$packages_page")
        else
            html=$(curl "https://github.com/$owner?tab=packages&visibility=public&&per_page=100&page=$packages_page")
        fi

        packages_lines=$(grep -zoP 'href="/'"$owner_type"'/'"$owner"'/packages/[^/]+/package/[^"]+"(.|\n)*href="/'"$owner"'/[^"]+"' <<<"$html" | tr -d '\0')
        [ -n "$packages_lines" ] || break

        while read -r line; do
            [ -n "$line" ] || continue
            package_new=$(cut -d'/' -f7 <<<"$line" | cut -d'"' -f1)
            package_type=$(cut -d'/' -f5 <<<"$line" | cut -d'"' -f1)
            repo=$(grep -oP 'href="/'"$owner"'/[^"]+"' <<<"$line" | cut -d'/' -f3 | cut -d'"' -f1)
            [ -n "$packages" ] && packages="$packages"$'\n'"$package_type/$repo/$package_new" || packages="$package_type/$repo/$package_new"
        done < <(echo "$packages_lines" | grep -zoP '(href="/'"$owner_type"'/'"$owner"'/packages/[^/]+/package/[^"]+"(.|\n)*?href="/'"$owner"'/[^"]+")' | tr -d '\0')
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
        [ -n "$package" ] || continue
        package_type=$(cut -d'/' -f1 <<<"$package_line")
        repo=$(cut -d'/' -f2 <<<"$package_line")
        package=$(cut -d'/' -f3 <<<"$package_line")

        # manual update: skip if the package is already in the index; the rest are updated daily
        if [ "$1" = "1" ]; then
            query="select count(*) from '$table_pkg_name' where owner_type='$owner_type' and package_type='$package_type' and owner='$owner' and repo='$repo' and package='$package';"
            count=$(sqlite3 "$INDEX_DB" "$query")
            ((count == 0)) || continue
        fi

        # scheduled update: skip if the package was updated today
        if [ "$1" = "0" ]; then
            query="select count(*) from '$table_pkg_name' where owner_type='$owner_type' and package_type='$package_type' and owner='$owner' and repo='$repo' and package='$package' and date='$TODAY';"
            count=$(sqlite3 "$INDEX_DB" "$query")
            ((count == 0)) || continue
        fi

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
        if sqlite3 "$INDEX_DB" ".tables" | grep -q "$table_version_name"; then
            query="select id, name from '$table_version_name';"
            while IFS='|' read -r id name; do
                if ! jq -e ".[] | select(.id == \"$id\")" <<<"$versions_json" &>/dev/null; then
                    versions_json=$(jq ". += [{\"id\":\"$id\",\"name\":\"$name\"}]" <<<"$versions_json")
                fi
            done < <(sqlite3 "$INDEX_DB" "$query")
        fi

        # limit to X pages / newest X00 versions
        while [ "$versions_page" -lt 1 ]; do
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

            version_size=-1
            version_id=$(_jq '.id')
            version_name=$(_jq '.name')
            version_tags=""

            if [ "$package_type" = "container" ]; then
                # get the size by adding up the layers
                [[ "$version_name" =~ ^sha256: ]] && sep="@" || sep=":"
                manifest=$(docker manifest inspect -v "ghcr.io/$lower_owner/$lower_package$sep$version_name")
                [[ ! "$manifest" =~ ^\[.*\]$ ]] || manifest=$(jq '.[]' <<<"$manifest")
                manifest=$(jq '.OCIManifest // .SchemaV2Manifest' <<<"$manifest")
                version_size=$(jq '.layers[].size' <<<"$manifest" | awk '{s+=$1} END {print s}')
                [[ "$version_size" =~ ^[0-9]+$ ]] || version_size=-1

                # to prevent can't iterate over null error, we need to check if the tags exist
                if jq -e '.metadata.container.tags' <<<"$manifest" &>/dev/null; then
                    for tag in $(_jq '.metadata.container.tags[]'); do
                        version_tags="$version_tags$tag,"
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

            # create a table for each package
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

            # if there is a row with the same id and date, replace it, otherwise insert a new row
            if [ "$count" -eq 0 ]; then
                query="insert into '$table_version_name' (id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('$version_id', '$version_name', '$version_size', '$version_raw_downloads', '$version_raw_downloads_month', '$version_raw_downloads_week', '$version_raw_downloads_day', '$TODAY', '$version_tags');"
            else
                query="update '$table_version_name' set size='$version_size', downloads='$version_raw_downloads', downloads_month='$version_raw_downloads_month', downloads_week='$version_raw_downloads_week', downloads_day='$version_raw_downloads_day', tags='$version_tags' where id='$version_id' and date='$TODAY';"
            fi

            sqlite3 "$INDEX_DB" "$query"
            check_limit
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

        # update stats
        query="select count(*) from '$table_pkg_name' where owner_type='$owner_type' and package_type='$package_type' and owner='$owner' and repo='$repo' and package='$package' and date='$TODAY';"
        count=$(sqlite3 "$INDEX_DB" "$query")

        if [ "$count" -eq 0 ]; then
            query="insert into '$table_pkg_name' (owner_id, owner_type, package_type, owner, repo, package, downloads, downloads_month, downloads_week, downloads_day, size, date) values ('$owner_id', '$owner_type', '$package_type', '$owner', '$repo', '$package', '$raw_downloads', '$raw_downloads_month', '$raw_downloads_week', '$raw_downloads_day', '$size', '$TODAY');"
        else
            query="update '$table_pkg_name' set owner_id='$owner_id', downloads='$raw_downloads', downloads_month='$raw_downloads_month', downloads_week='$raw_downloads_week', downloads_day='$raw_downloads_day', size='$size' where owner_type='$owner_type' and package_type='$package_type' and owner='$owner' and repo='$repo' and package='$package' and date='$TODAY';"
        fi

        sqlite3 "$INDEX_DB" "$query"
        check_limit
    done
done

# update index.json:
echo "[" >index.json
sqlite3 "$INDEX_DB" "select * from '$table_pkg_name' order by downloads + 0 desc;" | while IFS='|' read -r owner_type package_type owner repo package downloads downloads_month downloads_week downloads_day size date; do

    # only use the latest date for the package
    query="select max(date) from '$table_pkg_name' where owner_type='$owner_type' and package_type='$package_type' and owner='$owner' and repo='$repo' and package='$package';"
    max_date=$(sqlite3 "$INDEX_DB" "$query")
    [ "$date" = "$max_date" ] || continue

    fmt_downloads=$(numfmt <<<"$downloads")
    version_count=0
    version_with_tag_count=0
    table_version_name="versions_${owner_type}_${package_type}_${owner}_${repo}_${package}"
    printf "Refreshing %s/%s/%s (%s/%s)" "$owner" "$repo" "$package" "$owner_type" "$package_type"

    # get the version and tagged counts
    if sqlite3 "$INDEX_DB" ".tables" | grep -q "$table_version_name"; then
        query="select count(distinct id) from '$table_version_name';"
        version_count=$(sqlite3 "$INDEX_DB" "$query")
        query="select count(distinct id) from '$table_version_name' where tags != '' and tags is not null;"
        version_with_tag_count=$(sqlite3 "$INDEX_DB" "$query")
        printf " with %s versions (%s tagged)" "$version_count" "$version_with_tag_count"
    fi

    echo "..."
    echo "{" >>index.json
    [[ "$package_type" != "container" ]] || echo "\"image\": \"$package\",\"pulls\": \"$fmt_downloads\"," >>index.json
    echo "\"owner_type\": \"$owner_type\",
        \"package_type\": \"$package_type\",
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
        \"version\": [" >>index.json

    # add the versions to index.json
    if [ "$version_count" -gt 0 ]; then
        query="select id from '$table_version_name' order by id desc limit 1;"
        version_newest_id=$(sqlite3 "$INDEX_DB" "$query")

        # get only the last day each version was updated, which may not be today
        # desc sort by id
        query="select id, name, date, size, downloads, downloads_month, downloads_week, downloads_day, tags from '$table_version_name' group by id order by id desc;"
        sqlite3 "$INDEX_DB" "$query" | while IFS='|' read -r id name date size downloads downloads_month downloads_week downloads_day tags; do
            echo "{
                \"id\": $id,
                \"name\": \"$name\",
                \"date\": \"$date\",
                \"newest\": $([ "$id" = "$version_newest_id" ] && echo "true" || echo "false"),
                \"size\": \"$(numfmt_size <<<"$size")\",
                \"downloads\": \"$(numfmt <<<"$downloads")\",
                \"downloads_month\": \"$(numfmt <<<"$downloads_month")\",
                \"downloads_week\": \"$(numfmt <<<"$downloads_week")\",
                \"downloads_day\": \"$(numfmt <<<"$downloads_day")\",
                \"raw_size\": $size,
                \"raw_downloads\": $downloads,
                \"raw_downloads_month\": $downloads_month,
                \"raw_downloads_week\": $downloads_week,
                \"raw_downloads_day\": $downloads_day,
                \"tags\": [\"${tags//,/\",\"}\"]
                }," >>index.json

            # echo to stdout all the version stats on one line
            #printf "%s\t(%s)    \t%s/%s/%s/%s/%s\t%s\t%s\t%s\t%s\t%s\n" "$(numfmt <<<"$downloads")" "$downloads" "$owner_type" "$package_type" "$owner" "$repo" "$package" "$name" "$(numfmt_size <<<"$size")" "$(numfmt <<<"$downloads_month")" "$(numfmt <<<"$downloads_week")" "$(numfmt <<<"$downloads_day")"
        done
    fi

    # remove the last comma
    sed -i '$ s/,$//' index.json
    echo "]
    }," >>index.json
done

# remove the last comma
sed -i '$ s/,$//' index.json
echo "]" >>index.json

# run json through jq to format it
jq . index.json >index.tmp.json
mv index.tmp.json index.json

# sort the top level by raw_downloads
jq 'sort_by(.raw_downloads | tonumber) | reverse' index.json >index.tmp.json
mv index.tmp.json index.json

# minify the json
jq -c . index.json >index.tmp.json
mv index.tmp.json index.json

# update the README template with badges...
[ ! -f README.md ] || rm -f README.md # remove the old README
\cp .README.md README.md              # copy the template
perl -0777 -pe 's/<GITHUB_OWNER>/'"$GITHUB_OWNER"'/g; s/<GITHUB_REPO>/'"$GITHUB_REPO"'/g; s/<GITHUB_BRANCH>/'"$GITHUB_BRANCH"'/g' README.md >README.tmp && [ -f README.tmp ] && mv README.tmp README.md || :

echo "Total Downloads:"
sqlite3 "$INDEX_DB" "select * from '$table_pkg_name' order by downloads + 0 desc;" | while IFS='|' read -r owner_type package_type owner repo package downloads _ _ _ _ _; do
    export owner_type package_type owner repo package
    printf "%s\t(%s)\t%s/%s/%s (%s/%s)\n" "$(numfmt <<<"$downloads")" "$downloads" "$owner" "$repo" "$package" "$owner_type" "$package_type"

    # ...that have not been added yet
    grep -q "$owner_type/$package_type/$owner/$repo/$package" README.md || perl -0777 -pe '
    my $owner_type = $ENV{"owner_type"};
    my $package_type = $ENV{"package_type"};
    my $owner = $ENV{"owner"};
    my $repo = $ENV{"repo"};
    my $package = $ENV{"package"};
    my $thisowner = $ENV{"GITHUB_OWNER"};
    my $thisrepo = $ENV{"GITHUB_REPO"};
    my $thisbranch = $ENV{"GITHUB_BRANCH"};

    # decode percent-encoded characters
    for ($owner, $repo, $package) {
        s/%/%25/g;
    }
    my $label = $package;
    $label =~ s/%([0-9A-Fa-f]{2})/chr(hex($1))/eg;

    # add new badge
    s/\n\n(\[!\[.*)\n\n/\n\n$1 \[!\[$owner_type\/$package_type\/$owner\/$repo\/$package\]\(https:\/\/img.shields.io\/badge\/dynamic\/json\?url=https%3A%2F%2Fraw.githubusercontent.com%2F$thisowner%2F$thisrepo%2F$thisbranch%2Findex.json\&query=%24%5B%3F(%40.owner%3D%3D%22$owner%22%20%26%26%20%40.repo%3D%3D%22$repo%22%20%26%26%20%40.package%3D%3D%22$package%22)%5D.downloads\&label=$label\)\]\(https:\/\/github.com\/$owner\/$repo\/pkgs\/$package_type\/$package\)\n\n/g;
' README.md >README.tmp && [ -f README.tmp ] && mv README.tmp README.md || :
done

echo "Done!"
