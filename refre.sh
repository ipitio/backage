#!/bin/bash
# Update the templates
# Usage: ./refre.sh
# Dependencies: jq, sqlite3
# Copyright (c) ipitio
#
# shellcheck disable=SC1091,SC2015

source lib.sh
[ ! -f README.md ] || rm -f README.md # remove the old README
\cp templates/.README.md README.md    # copy the template
perl -0777 -pe 's/<GITHUB_OWNER>/'"$GITHUB_OWNER"'/g; s/<GITHUB_REPO>/'"$GITHUB_REPO"'/g; s/<GITHUB_BRANCH>/'"$GITHUB_BRANCH"'/g' README.md >README.tmp && [ -f README.tmp ] && mv README.tmp README.md || :
echo "Total Downloads:"
echo "[" >index.json

sqlite3 "$INDEX_DB" "select * from '$$BKG_INDEX_TBL_PKG' order by downloads + 0 desc;" | while IFS='|' read -r owner_id owner_type package_type owner repo package downloads downloads_month downloads_week downloads_day size date; do
    script_now=$(date +%s)
    script_diff=$((script_now - SCRIPT_START))

    if ((script_diff >= 21500)); then
        echo "Script has been running for 6 hours. Committing changes..."
        break
    fi

    # only use the latest date for the package
    query="select date from '$$BKG_INDEX_TBL_PKG' where owner_type='$owner_type' and package_type='$package_type' and owner='$owner' and repo='$repo' and package='$package' order by date desc limit 1;"
    max_date=$(sqlite3 "$INDEX_DB" "$query")
    [ "$date" = "$max_date" ] || continue

    fmt_downloads=$(numfmt <<<"$downloads")
    version_count=0
    version_with_tag_count=0
    table_version_name="versions_${owner_type}_${package_type}_${owner}_${repo}_${package}"

    # get the version and tagged counts
    query="select name from sqlite_master where type='table' and name='$table_version_name';"
    table_exists=$(sqlite3 "$INDEX_DB" "$query")

    if [ -n "$table_exists" ]; then
        query="select count(distinct id) from '$table_version_name';"
        version_count=$(sqlite3 "$INDEX_DB" "$query")
        query="select count(distinct id) from '$table_version_name' where tags != '' and tags is not null;"
        version_with_tag_count=$(sqlite3 "$INDEX_DB" "$query")
    fi

    echo "{" >>index.json
    [[ "$package_type" != "container" ]] || echo "\"image\": \"$package\",\"pulls\": \"$fmt_downloads\"," >>index.json
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
        \"version\": [" >>index.json

    # add the versions to index.json
    if [ "$version_count" -gt 0 ]; then
        query="select id from '$table_version_name' order by id desc limit 1;"
        version_newest_id=$(sqlite3 "$INDEX_DB" "$query")

        # get only the last day each version was updated, which may not be today
        # desc sort by id
        query="select id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags from '$table_version_name' group by id order by id desc;"
        sqlite3 "$INDEX_DB" "$query" | while IFS='|' read -r vid vname vsize vdownloads vdownloads_month vdownloads_week vdownloads_day vdate vtags; do
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
                }," >>index.json
        done
    fi

    # remove the last comma
    sed -i '$ s/,$//' index.json
    echo "]
    }," >>index.json

    export owner_type package_type owner repo package
    printf "%s\t(%s)\t%s/%s/%s (%s/%s)\n" "$(numfmt <<<"$downloads")" "$downloads" "$owner" "$repo" "$package" "$owner_type" "$package_type"

    [ "$downloads" -ge 100000000 ] || continue
    grep -q "$owner_type/$package_type/$owner/$repo/$package" README.md || perl -0777 -pe '
    my $owner_type = $ENV{"owner_type"};
    my $package_type = $ENV{"package_type"};
    my $owner = $ENV{"owner"};
    my $repo = $ENV{"repo"};
    my $package = $ENV{"package"};
    my $thisowner = $ENV{"GITHUB_OWNER"};
    my $thisrepo = $ENV{"GITHUB_REPO"};
    my $thisbranch = $ENV{"GITHUB_BRANCH"};
    my $label = $package;

    # decode percent-encoded characters
    for ($owner, $repo, $label) {
        s/%/%25/g;
    }

    $label =~ s/%([0-9A-Fa-f]{2})/chr(hex($1))/eg;

    # add new badge
    s/\n\n(\[!\[.*)\n\n/\n\n$1 \[!\[$owner_type\/$package_type\/$owner\/$repo\/$package\]\(https:\/\/img.shields.io\/badge\/dynamic\/json\?url=https%3A%2F%2Fraw.githubusercontent.com%2F$thisowner%2F$thisrepo%2F$thisbranch%2Findex.json\&query=%24%5B%3F(%40.owner%3D%3D%22$owner%22%20%26%26%20%40.repo%3D%3D%22$repo%22%20%26%26%20%40.package%3D%3D%22$package%22)%5D.downloads\&label=$label\)\]\(https:\/\/github.com\/$owner\/$repo\/pkgs\/$package_type\/$package\)\n\n/g;
' README.md >README.tmp && [ -f README.tmp ] && mv README.tmp README.md || :
done

# remove the last comma
sed -i '$ s/,$//' index.json
echo "]" >>index.json

# sort the top level by raw_downloads
jq -c 'sort_by(.raw_downloads | tonumber) | reverse' index.json >index.tmp.json
mv index.tmp.json index.json

# if the json is over 100MB, remove oldest versions from the packages with the most versions
json_size=$(stat -c %s index.json)
while [ "$json_size" -gt 100000000 ]; do
    jq -c 'sort_by(.versions | tonumber) | reverse | map(select(.versions > 0)) | map(.version |= sort_by(.id | tonumber) | del(.version[0]))' index.json >index.tmp.json
    mv index.tmp.json index.json
    json_size=$(stat -c %s index.json)
done

# copy the CHANGELOG template and update the version
\cp templates/.CHANGELOG.md CHANGELOG.md
query="select count(distinct owner) from '$$BKG_INDEX_TBL_PKG';"
owners=$(sqlite3 "$INDEX_DB" "$query")
query="select count(distinct repo) from '$$BKG_INDEX_TBL_PKG';"
repos=$(sqlite3 "$INDEX_DB" "$query")
query="select count(distinct package) from '$$BKG_INDEX_TBL_PKG';"
packages=$(sqlite3 "$INDEX_DB" "$query")
html=$(curl -s "https://github.com/$GITHUB_OWNER/$GITHUB_REPO/releases/latest")
raw_assets=$(grep -Pzo 'Assets[^"]*"\d*' <<<"$html" | grep -Pzo '\d*$' | tr -d '\0')
perl -0777 -pe 's/\[VERSION\]/'"$BKG_VERSION"'/g; s/\[OWNERS\]/'"$owners"'/g; s/\[REPOS\]/'"$repos"'/g; s/\[PACKAGES\]/'"$packages"'/g' CHANGELOG.md >CHANGELOG.tmp && [ -f CHANGELOG.tmp ] && mv CHANGELOG.tmp CHANGELOG.md || :
[ -n "$raw_assets" ] && [ "$raw_assets" -ge 4 ] && echo " The database grew over 2GB and was rotated, but you can find all previous data under [Releases](https://github.com/$GITHUB_OWNER/$GITHUB_REPO/releases)." >>CHANGELOG.md || :
exit 2
