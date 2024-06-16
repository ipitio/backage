#!/bin/bash
# Update the stats for each package in pkg.txt
# Usage: ./update.sh
# Dependencies: curl, jq, sqlite3, Docker
# Copyright (c) ipitio
#
# shellcheck disable=SC2015

declare -r INDEX_DB="index.db" # sqlite
declare TODAY
TODAY=$(date -u +%Y-%m-%d)
readonly TODAY

# format numbers like 1000 to 1k
numfmt() {
    awk '{ split("k M B T P E Z Y", v); s=0; while( $1>999.9 ) { $1/=1000; s++ } print int($1*10)/10 v[s] }'
}

# format bytes to KB, MB, GB, etc.
numfmtSize() {
    awk '{ split("B kB MB GB TB PB EB ZB YB", v); s=0; while( $1>999.9 ) { $1/=1000; s++ } print int($1*10)/10 " " v[s] }'
}

# check if curl and jq are installed
if ! command -v curl &>/dev/null || ! command -v jq &>/dev/null || ! command -v sqlite3 &>/dev/null; then
    sudo apt-get update
    sudo apt-get install curl jq sqlite3 -y
fi

# clean pkg.txt
awk '{print tolower($0)}' pkg.txt | sort -u | while read -r line; do
    grep -i "^$line$" pkg.txt
done >pkg.tmp.txt
mv pkg.tmp.txt pkg.txt
[ -z "$(tail -c 1 pkg.txt)" ] || echo >>pkg.txt

# use a sqlite database to store the downloads of a package
[ -f "$INDEX_DB" ] || touch "$INDEX_DB"
table_pkg_name="packages"
table_pkg="create table if not exists '$table_pkg_name' (
    owner_type text not null,
    package_type text not null,
    owner text not null,
    repo text not null,
    package text not null,
    downloads text not null,
    downloads_month text not null,
    downloads_week text not null,
    downloads_day text not null,
    size text not null,
    date text not null,
    primary key (owner_type, package_type, owner, repo, package)
);"
sqlite3 "$INDEX_DB" "$table_pkg"

rate_limit_start=$(date +%s)
calls_to_api=0
while IFS= read -r line; do
    owner_type=$(cut -d'/' -f1 <<<"$line")
    owner=$(cut -d'/' -f3 <<<"$line")
    package_type=$(cut -d'/' -f2 <<<"$line")
    repo=$(cut -d'/' -f4 <<<"$line")
    package=$(cut -d'/' -f5 <<<"$line")
    downloads=-1
    raw_downloads=-1
    size="0"

    # manual update: skip if the package is already in the index; the rest are updated on a consistent basis
    if [ "$1" = "1" ]; then
        # check if the package is already in the database and was updated in the last 24 hours
        query="select count(*) from '$table_pkg_name' where owner_type='$owner_type' and package_type='$package_type' and owner='$owner' and repo='$repo' and package='$package' and date in ('$TODAY', date('$TODAY', '-1 day'));"
        count=$(sqlite3 "$INDEX_DB" "$query")
        ((count == 0)) || continue
    fi

    # scheduled update: skip if the package was updated in the last 12 hours
    if [ "$1" = "0" ]; then
        query="select count(*) from '$table_pkg_name' where owner_type='$owner_type' and package_type='$package_type' and owner='$owner' and repo='$repo' and package='$package' and date in ('$TODAY', date('$TODAY', '-12 hours'));"
        count=$(sqlite3 "$INDEX_DB" "$query")
        ((count == 0)) || continue
    fi

    echo "Updating $owner/$repo/$package ($owner_type/$package_type)..."

    # scrape the package page for the total downloads
    html=$(curl -sSLNZ "https://github.com/$owner/$repo/pkgs/$package_type/$package" | tr -d '\0')
    raw_downloads=$(grep -Pzo '(?<=Total downloads</span>\n          <h3 title=")\d*' <<<"$html") # is this the same for all types?
    [[ ! "$raw_downloads" =~ ^[0-9]+$ ]] || downloads=$(numfmt <<<"$raw_downloads")
    versions_json="[{\"id\":\"latest\",\"name\":\"latest\"}]" # default to latest
    is_public=$(grep -Pzo 'Total downloads' <<<"$html")

    # if the repo is piblic the api request should succeed
    if [ -n "$GITHUB_TOKEN" ] && [ -n "$is_public" ]; then
        versions_json=$(curl -L \
            -H "Accept: application/vnd.github+json" \
            -H "Authorization: Bearer $GITHUB_TOKEN" \
            -H "X-GitHub-Api-Version: 2022-11-28" \
            "https://api.github.com/$owner_type/$owner/packages/$package_type/$package/versions")
        ((calls_to_api++))
    fi

    # decode percent-encoded characters and make lowercase for docker manifest
    if [ "$package_type" = "container" ]; then
        lower_owner=$owner
        lower_package=$package

        for i in "$lower_owner" "$lower_package"; do
            i=${i//%/%25}
        done

        lower_owner=$(perl -pe 's/%([0-9A-Fa-f]{2})/chr(hex($1))/eg' <<<"$lower_owner" | tr '[:upper:]' '[:lower:]')
        lower_package=$(perl -pe 's/%([0-9A-Fa-f]{2})/chr(hex($1))/eg' <<<"$lower_package" | tr '[:upper:]' '[:lower:]')
    fi

    # scan the versions
    for i in $(jq -r '.[] | @base64' <<<"$versions_json"); do
        _jq() {
            echo "$i" | base64 --decode | jq -r "$@"
        }

        version_size="0"
        version_id=$(_jq '.id')
        version_name=$(_jq '.name')

        # get the size
        if [ "$package_type" = "container" ]; then
            # if version_name begins with sha256:, it's a digest
            if [[ "$version_name" =~ ^sha256: ]]; then
                version_size=$(docker manifest inspect -v ghcr.io/"$lower_owner"/"$lower_package"@"$version_name" | jq '.Descriptor.size')
            else
                version_size=$(docker manifest inspect -v ghcr.io/"$lower_owner"/"$lower_package":"$version_name" | grep size | awk -F ':' '{sum+=$NF} END {print sum}')
            fi

            [[ "$version_size" =~ ^[0-9]+$ ]] || version_size="0"
        fi

        # get the downloads
        version_html=$(curl -sSLNZ "https://github.com/$owner/$repo/pkgs/$package_type/$package/$version_id" | tr -d '\0')
        version_raw_downloads=$(echo "$version_html" | grep -Pzo 'Total downloads<[^<]*<[^<]*' | grep -Pzo '\d*$')

        if [[ "$version_raw_downloads" =~ ^[0-9]+$ ]]; then
            version_raw_downloads_month=$(grep -Pzo 'Last 30 days<[^<]*<[^<]*' <<<"$version_html" | grep -Pzo '\d*$')
            version_raw_downloads_week=$(grep -Pzo 'Last week<[^<]*<[^<]*' <<<"$version_html" | grep -Pzo '\d*$')
            version_raw_downloads_day=$(grep -Pzo 'Today<[^<]*<[^<]*' <<<"$version_html" | grep -Pzo '\d*$')
        else
            version_raw_downloads=-1
            version_raw_downloads_month=-1
            version_raw_downloads_week=-1
            version_raw_downloads_day=-1
        fi

        # create a table for each package
        table_version_name="versions_${owner}_${repo}_${package}"
        table_version="create table if not exists '$table_version_name' (
            id text not null,
            name text not null,
            size text not null,
            downloads text not null,
            downloads_month text not null,
            downloads_week text not null,
            downloads_day text not null,
            date text not null,
            primary key (id, date)
        );"
        sqlite3 "$INDEX_DB" "$table_version"
        search="select count(*) from '$table_version_name' where id='$version_id' and date='$TODAY';"
        count=$(sqlite3 "$INDEX_DB" "$search")

        # if there is a row with the same id and date, replace it, otherwise insert a new row
        if [ "$count" -eq 0 ]; then
            query="insert into '$table_version_name' (id, name, size, downloads, downloads_month, downloads_week, downloads_day, date) values ('$version_id', '$version_name', '$version_size', '$version_raw_downloads', '$version_raw_downloads_month', '$version_raw_downloads_week', '$version_raw_downloads_day', '$TODAY');"
        else
            query="update '$table_version_name' set size='$version_size', downloads='$version_raw_downloads', downloads_month='$version_raw_downloads_month', downloads_week='$version_raw_downloads_week', downloads_day='$version_raw_downloads_day' where id='$version_id' and date='$TODAY';"
        fi

        sqlite3 "$INDEX_DB" "$query"
    done

    # calculate the total downloads over all versions for day, week, and month using sqlite:
    query="select sum(downloads), sum(downloads_month), sum(downloads_week), sum(downloads_day) from 'versions_${owner}_${repo}_${package}' where date='$TODAY';"
    summed_raw_downloads=$(sqlite3 "$INDEX_DB" "$query" | cut -d'|' -f1)
    raw_downloads_month=$(sqlite3 "$INDEX_DB" "$query" | cut -d'|' -f2)
    raw_downloads_week=$(sqlite3 "$INDEX_DB" "$query" | cut -d'|' -f3)
    raw_downloads_day=$(sqlite3 "$INDEX_DB" "$query" | cut -d'|' -f4)
    [ "$summed_raw_downloads" -eq "$raw_downloads" ] || echo "Total Downloads Discrepancy: $raw_downloads given != $summed_raw_downloads summed across versions."

    # update total downloads
    query="select count(*) from '$table_pkg_name' where owner_type='$owner_type' and package_type='$package_type' and owner='$owner' and repo='$repo' and package='$package';"
    count=$(sqlite3 "$INDEX_DB" "$query")

    # use the latest version's size as the package size
    query="select id from 'versions_${owner}_${repo}_${package}' order by id desc limit 1;"
    version_latest_id=$(sqlite3 "$INDEX_DB" "$query")
    query="select size from 'versions_${owner}_${repo}_${package}' where id='$version_latest_id';"
    size=$(sqlite3 "$INDEX_DB" "$query")

    if [ "$count" -eq 0 ]; then
        query="insert into '$table_pkg_name' (owner_type, package_type, owner, repo, package, downloads, downloads_month, downloads_week, downloads_day, size, date) values ('$owner_type', '$package_type', '$owner', '$repo', '$package', '$raw_downloads', '$raw_downloads_month', '$raw_downloads_week', '$raw_downloads_day', '$size', '$TODAY');"
    else
        query="update '$table_pkg_name' set downloads='$raw_downloads', downloads_month='$raw_downloads_month', downloads_week='$raw_downloads_week', downloads_day='$raw_downloads_day', size='$size' where owner_type='$owner_type' and package_type='$package_type' and owner='$owner' and repo='$repo' and package='$package';"
    fi

    sqlite3 "$INDEX_DB" "$query"

    # api rate limit, the next run will take care of the rest
    rate_limit_end=$(date +%s)
    rate_limit_diff=$((rate_limit_end - rate_limit_start))
    [ "$rate_limit_diff" -lt 3500 ] || break # break if we're approaching an hour
    [ "$calls_to_api" -lt 1000 ] || break    # break if we've made 1000 calls
done <pkg.txt

# update index.json:
echo "[" >index.json
sqlite3 "$INDEX_DB" "select * from '$table_pkg_name' order by downloads + 0 desc;" | while IFS='|' read -r owner_type package_type owner repo package downloads downloads_month downloads_week downloads_day size date; do
    pretty_downloads=$(numfmt <<<"$downloads")
    pretty_downloads_month=$(numfmt <<<"$downloads_month")
    pretty_downloads_week=$(numfmt <<<"$downloads_week")
    pretty_downloads_day=$(numfmt <<<"$downloads_day")
    pretty_size=$(numfmtSize <<<"$size")

    query="select id from 'versions_${owner}_${repo}_${package}' order by id desc limit 1;"
    version_latest_id=$(sqlite3 "$INDEX_DB" "$query")
    query="select name from 'versions_${owner}_${repo}_${package}' where id='$version_latest_id';"
    version_latest=$(sqlite3 "$INDEX_DB" "$query")
    query="select count(*) from 'versions_${owner}_${repo}_${package}';"
    version_count=$(sqlite3 "$INDEX_DB" "$query")
    version_count_pretty=$(numfmt <<<"$version_count")

    # if package_type is container, add "image" and "pulls" for backwards compatibility
    # please use "package" and "downloads" instead
    if [ "$package_type" = "container" ]; then
        echo "{
            \"owner_type\": \"$owner_type\",
            \"package_type\": \"$package_type\",
            \"owner\": \"$owner\",
            \"repo\": \"$repo\",
            \"package\": \"$package\",
            \"image\": \"$package\",
            \"version_latest\": \"$version_latest\",
            \"version_count\": \"$version_count_pretty\",
            \"raw_version_count\": \"$version_count\",
            \"size\": \"$pretty_size\",
            \"raw_size\": \"$size\",
            \"pulls\": \"$pretty_downloads\",
            \"downloads\": \"$pretty_downloads\",
            \"downloads_month\": \"$pretty_downloads_month\",
            \"downloads_week\": \"$pretty_downloads_week\",
            \"downloads_day\": \"$pretty_downloads_day\",
            \"raw_downloads\": \"$downloads\",
            \"raw_downloads_month\": \"$downloads_month\",
            \"raw_downloads_week\": \"$downloads_week\",
            \"raw_downloads_day\": \"$downloads_day\",
            \"date\": \"$date\",
            \"versions\": [" >>index.json
    else
        echo "{
            \"owner_type\": \"$owner_type\",
            \"package_type\": \"$package_type\",
            \"owner\": \"$owner\",
            \"repo\": \"$repo\",
            \"package\": \"$package\",
            \"version_latest\": \"$version_latest\",
            \"version_count\": \"$version_count_pretty\",
            \"raw_version_count\": \"$version_count\",
            \"size\": \"$pretty_size\",
            \"raw_size\": \"$size\",
            \"downloads\": \"$pretty_downloads\",
            \"downloads_month\": \"$pretty_downloads_month\",
            \"downloads_week\": \"$pretty_downloads_week\",
            \"downloads_day\": \"$pretty_downloads_day\",
            \"raw_downloads\": \"$downloads\",
            \"raw_downloads_month\": \"$downloads_month\",
            \"raw_downloads_week\": \"$downloads_week\",
            \"raw_downloads_day\": \"$downloads_day\",
            \"date\": \"$date\",
            \"versions\": [" >>index.json
    fi

    # add the versions to index.json
    sqlite3 "$INDEX_DB" "select * from 'versions_${owner}_${repo}_${package}' where date='$TODAY';" | while IFS='|' read -r id name size downloads downloads_month downloads_week downloads_day date; do
        pretty_downloads=$(numfmt <<<"$downloads")
        pretty_downloads_month=$(numfmt <<<"$downloads_month")
        pretty_downloads_week=$(numfmt <<<"$downloads_week")
        pretty_downloads_day=$(numfmt <<<"$downloads_day")
        pretty_size=$(numfmtSize <<<"$size")

        echo "{
            \"id\": \"$id\",
            \"name\": \"$name\",
            \"size\": \"$pretty_size\",
            \"raw_size\": \"$size\",
            \"downloads\": \"$pretty_downloads\",
            \"downloads_month\": \"$pretty_downloads_month\",
            \"downloads_week\": \"$pretty_downloads_week\",
            \"downloads_day\": \"$pretty_downloads_day\",
            \"raw_downloads\": \"$downloads\",
            \"raw_downloads_month\": \"$downloads_month\",
            \"raw_downloads_week\": \"$downloads_week\",
            \"raw_downloads_day\": \"$downloads_day\",
            \"date\": \"$date\"
            }," >>index.json
    done
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

# update the README template with badges...
[ ! -f README.md ] || rm -f README.md # remove the old README
\cp .README.md README.md              # copy the template
echo "Total Downloads:"
sqlite3 "$INDEX_DB" "select * from '$table_pkg_name' order by downloads + 0 desc;" | while IFS='|' read -r owner_type package_type owner repo package downloads _ _ _ _ _; do
    export owner_type package_type owner repo package
    pretty_downloads=$(numfmt <<<"$downloads")
    printf "%s\t(%s)    \t%s/%s/%s (%s/%s)\n" "$pretty_downloads" "$downloads" "$owner" "$repo" "$package" "$owner_type" "$package_type"

    # ...that have not been added yet
    grep -q "$owner_type/$package_type/$owner/$repo/$package" README.md || perl -0777 -pe '
    my $owner_type = $ENV{"owner_type"};
    my $package_type = $ENV{"package_type"};
    my $owner = $ENV{"owner"};
    my $repo = $ENV{"repo"};
    my $package = $ENV{"package"};

    # decode percent-encoded characters
    for ($owner, $repo, $package) {
        s/%/%25/g;
    }
    my $label = $package;
    $label =~ s/%([0-9A-Fa-f]{2})/chr(hex($1))/eg;

    # add new badge
    s/\n\n(\[!\[.*)\n\n/\n\n$1 \[!\[$owner_type\/$package_type\/$owner\/$repo\/$package\]\(https:\/\/img.shields.io\/badge\/dynamic\/json\?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json\&query=%24%5B%3F(%40.owner%3D%3D%22$owner%22%20%26%26%20%40.repo%3D%3D%22$repo%22%20%26%26%20%40.package%3D%3D%22$package%22)%5D.downloads\&label=$label\)\]\(https:\/\/github.com\/$owner\/$repo\/pkgs\/container\/$package\)\n\n/g;
' README.md >README.tmp && [ -f README.tmp ] && mv README.tmp README.md || :
done
