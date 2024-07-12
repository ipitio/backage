#!/bin/bash
# Refresh json and README
# Usage: ./refre.sh
# Dependencies: jq, sqlite3
# Copyright (c) ipitio
#
# shellcheck disable=SC1091,SC2015

source lib.sh

# shellcheck disable=SC2317
refresh_owner() {
    owner=$1
    [ -n "$owner" ] || return
    # create the owner's json file
    echo "[" >index/"$owner".json

    # go through each package in the index
    sqlite3 "$(get_BKG BKG_INDEX_DB)" "select * from '$(get_BKG BKG_INDEX_TBL_PKG)' where owner='$owner' order by downloads + 0 asc;" | while IFS='|' read -r owner_id owner_type package_type _ repo package downloads downloads_month downloads_week downloads_day size date; do
        script_now=$(date -u +%s)
        script_diff=$((script_now - SCRIPT_START))

        if ((script_diff >= 21500)); then
            echo "Script has been running for 6 hours. Committing changes..."
            break
        fi

        # only use the latest date for the package
        query="select date from '$(get_BKG BKG_INDEX_TBL_PKG)' where owner_type='$owner_type' and package_type='$package_type' and owner_id='$owner_id' and repo='$repo' and package='$package' order by date desc limit 1;"
        max_date=$(sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query")
        [ "$date" = "$max_date" ] || continue

        fmt_downloads=$(numfmt <<<"$downloads")
        version_count=0
        version_with_tag_count=0
        table_version_name="versions_${owner_type}_${package_type}_${owner}_${repo}_${package}"

        # get the version and tagged counts
        query="select name from sqlite_master where type='table' and name='$table_version_name';"
        table_exists=$(sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query")

        if [ -n "$table_exists" ]; then
            query="select count(distinct id) from '$table_version_name';"
            version_count=$(sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query")
            query="select count(distinct id) from '$table_version_name' where tags != '' and tags is not null;"
            version_with_tag_count=$(sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query")
        fi

        echo "{" >>index/"$owner".json
        [[ "$package_type" != "container" ]] || echo "\"image\": \"$package\",\"pulls\": \"$fmt_downloads\"," >>index/"$owner".json
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
            \"version\": [" >>index/"$owner".json

        # add the versions to index/"$owner".json
        if [ "$version_count" -gt 0 ]; then
            query="select id from '$table_version_name' order by id desc limit 1;"
            version_newest_id=$(sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query")

            # get only the last day each version was updated, which may not be today
            # desc sort by id
            query="select id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags from '$table_version_name' group by id order by id desc;"
            sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query" | while IFS='|' read -r vid vname vsize vdownloads vdownloads_month vdownloads_week vdownloads_day vdate vtags; do
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
                    }," >>index/"$owner".json
            done
        fi

        # remove the last comma
        sed -i '$ s/,$//' index/"$owner".json
        echo "]
        }," >>index/"$owner".json
    done

    # remove the last comma
    sed -i '$ s/,$//' index/"$owner".json
    echo "]" >>index/"$owner".json

    # if the json is empty, exit
    jq -e 'length > 0' index/"$owner".json || return

    # sort the top level by raw_downloads
    jq -c 'sort_by(.raw_downloads | tonumber) | reverse' index/"$owner".json >index/"$owner".tmp.json
    mv index/"$owner".tmp.json index/"$owner".json

    # if the json is over 50MB, remove oldest versions from the packages with the most versions
    json_size=$(stat -c %s index/"$owner".json)
    while [ "$json_size" -gt 50000000 ]; do
        jq -e 'map(.version | length > 0) | any' index/"$owner".json || break
        jq -c 'sort_by(.versions | tonumber) | reverse | map(select(.versions > 0)) | map(.version |= sort_by(.id | tonumber) | del(.version[0]))' index/"$owner".json >index.tmp.json
        mv index.tmp.json index/"$owner".json
        json_size=$(stat -c %s index/"$owner".json)
    done
}

# refresh the files
[ ! -f README.md ] || rm -f README.md # remove the old README
\cp templates/.README.md README.md    # copy the template
perl -0777 -pe 's/<GITHUB_OWNER>/'"$GITHUB_OWNER"'/g; s/<GITHUB_REPO>/'"$GITHUB_REPO"'/g; s/<GITHUB_BRANCH>/'"$GITHUB_BRANCH"'/g' README.md >README.tmp && [ -f README.tmp ] && mv README.tmp README.md || :
[ -d index ] || mkdir index
owners=$(sqlite3 "$(get_BKG BKG_INDEX_DB)" "select distinct owner from '$(get_BKG BKG_INDEX_TBL_PKG)';")
echo "$owners" | env_parallel -j 2000% --bar refresh_owner >/dev/null

for owner in $owners; do
    if [ ! -f index/"$owner".json ] || jq -e 'length == 0' index/"$owner".json; then
        exit 1
    fi
done
