#!/bin/bash
# shellcheck disable=SC1091,SC2015,SC2154

source lib/util.sh

save_version() {
    [ -n "$1" ] || return
    local version_id
    local version_name
    local version_tags
    version_id=$(_jq "$1" '.id')
    version_name=$(_jq "$1" '.name')
    [[ "$version_id" =~ ^[0-9]+$ ]] || version_id="-1"

    if [ -f "${table_version_name}"_already_updated ]; then
        check_limit || return $?
        ! grep -q "$version_id" "${table_version_name}"_already_updated || return
        version_tags=$(_jq "$1" '.. | try .tags | join(",")')
        [ -n "$version_tags" ] || version_tags=$(_jq "$1" '.. | try .tags')
        echo "{
            \"id\": $version_id,
            \"name\": \"$version_name\",
            \"tags\": \"$version_tags\"
        }" | tr -d '\n' | jq -c . >"$BKG_INDEX_DIR/$owner/$repo/$package.$version_id.json" || echo "Failed to save $owner/$repo/$package/$version_id"
    else
        local version_size
        local version_dl
        local version_dl_month
        local version_dl_week
        local version_dl_day
        version_size=$(_jq "$1" '.size')
        version_dl=$(_jq "$1" '.downloads')
        version_dl_month=$(_jq "$1" '.downloads_month')
        version_dl_week=$(_jq "$1" '.downloads_week')
        version_dl_day=$(_jq "$1" '.downloads_day')
        version_tags=$(_jq "$1" '.tags')
        [[ "$version_size" =~ ^[0-9]+$ ]] || version_size="-1"
        [[ "$version_dl" =~ ^[0-9]+$ ]] || version_dl="-1"
        [[ "$version_dl_month" =~ ^[0-9]+$ ]] || version_dl_month="-1"
        [[ "$version_dl_week" =~ ^[0-9]+$ ]] || version_dl_week="-1"
        [[ "$version_dl_day" =~ ^[0-9]+$ ]] || version_dl_day="-1"

        echo "{
            \"id\": $version_id,
            \"name\": \"$version_name\",
            \"date\": \"$(date -u +%Y-%m-%d)\",
            \"newest\": false,
            \"latest\": false,
            \"size\": \"$(numfmt_size <<<"$version_size")\",
            \"downloads\": \"$(numfmt <<<"$version_dl")\",
            \"downloads_month\": \"$(numfmt <<<"$version_dl_month")\",
            \"downloads_week\": \"$(numfmt <<<"$version_dl_week")\",
            \"downloads_day\": \"$(numfmt <<<"$version_dl_day")\",
            \"raw_size\": $version_size,
            \"raw_downloads\": $version_dl,
            \"raw_downloads_month\": $version_dl_month,
            \"raw_downloads_week\": $version_dl_week,
            \"raw_downloads_day\": $version_dl_day,
            \"tags\": [\"${version_tags//,/\",\"}\"]
        }" | tr -d '\n' | jq -c . >"$BKG_INDEX_DIR/$owner/$repo/$package.d/$version_id.json" || echo "Failed to refresh $owner/$repo/$package/$version_id"
    fi
}

page_version() {
    check_limit || return $?
    [ -n "$1" ] || return
    local versions_json_more="[]"
    local calls_to_api
    local min_calls_to_api
    local version_lines

    if [ -n "$GITHUB_TOKEN" ]; then
        echo "Starting $owner/$package page $1..."
        versions_json_more=$(curl -H "Accept: application/vnd.github+json" \
            -H "Authorization: Bearer $GITHUB_TOKEN" \
            -H "X-GitHub-Api-Version: 2022-11-28" \
            "https://api.github.com/$owner_type/$owner/packages/$package_type/$package/versions?per_page=100&page=$1")
        (($? != 3)) || return 3
        calls_to_api=$(get_BKG BKG_CALLS_TO_API)
        min_calls_to_api=$(get_BKG BKG_MIN_CALLS_TO_API)
        ((calls_to_api++))
        ((min_calls_to_api++))
        set_BKG BKG_CALLS_TO_API "$calls_to_api"
        set_BKG BKG_MIN_CALLS_TO_API "$min_calls_to_api"
    fi

    jq -e '.[].id' <<<"$versions_json_more" &>/dev/null || return 2
    version_lines=$(jq -r '.[] | @base64' <<<"$versions_json_more")
    run_parallel save_version "$version_lines"
    (($? != 3)) || return 3
    echo "Started $owner/$package page $1"
    # if there are fewer than 100 lines, break
    [ "$(wc -l <<<"$version_lines")" -eq 100 ] || return 2
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
    local version_id
    local manifest
    local sep
    local today
    today=$(date -u +%Y-%m-%d)
    version_id=$(_jq "$1" '.id')
    version_name=$(_jq "$1" '.name')
    version_tags=$(_jq "$1" '.tags')
    echo "Updating $owner/$package/$version_id..."
    version_html=$(curl "https://github.com/$owner/$repo/pkgs/$package_type/$package/$version_id")
    (($? != 3)) || return 3
    version_raw_downloads=$(echo "$version_html" | grep -Pzo 'Total downloads<[^<]*<[^<]*' | grep -Pzo '\d*$' | tr -d '\0' | tr -d ',')

    if [[ "$version_raw_downloads" =~ ^[0-9]+$ ]]; then
        version_raw_downloads_month=$(grep -Pzo 'Last 30 days<[^<]*<[^<]*' <<<"$version_html" | grep -Pzo '\d*$' | tr -d '\0' | tr -d ',')
        version_raw_downloads_week=$(grep -Pzo 'Last week<[^<]*<[^<]*' <<<"$version_html" | grep -Pzo '\d*$' | tr -d '\0' | tr -d ',')
        version_raw_downloads_day=$(grep -Pzo 'Today<[^<]*<[^<]*' <<<"$version_html" | grep -Pzo '\d*$' | tr -d '\0' | tr -d ',')
    else
        version_raw_downloads=-1
    fi

    if [ "$package_type" = "container" ]; then
        manifest=$(tr -d '\n' <<<"$version_html" | grep -Pzo '<code.*?>.*?<pre.*?>\K[^<]*(?=<\/pre>)' | tr -d '\0' | sed 's/&quot;/"/g')

        if [ -z "$manifest" ] || ! jq -e '.' <<<"$manifest" &>/dev/null; then
            [[ "$version_name" =~ ^sha256:.+$ ]] && sep="@" || sep=":"
            manifest=$(docker manifest inspect -v "ghcr.io/$lower_owner/$lower_package$sep$version_name" 2>&1)
        fi

        # get the size by adding up the layers
        if [[ -n "$(jq '.. | try .layers[]' 2>/dev/null <<<"$manifest")" ]]; then
            version_size=$(jq '.. | try .size | select(. > 0)' <<<"$manifest" | awk '{s+=$1} END {print s}')
            [[ "$version_size" =~ ^[0-9]+$ ]] || version_size=-1
        elif [[ -n "$(jq '.. | try .manifests[]' 2>/dev/null <<<"$manifest")" ]]; then
            version_size=$(jq '.. | try .size | select(. > 0)' <<<"$manifest" | awk '{s+=$1} END {print s/NR}')
            [[ "$version_size" =~ ^[0-9]+$ ]] || version_size=-1
        fi
    else
        : # TODO: get size for other package types
    fi

    sqlite3 "$BKG_INDEX_DB" "insert or replace into '$table_version_name' (id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('$version_id', '$version_name', '$version_size', '$version_raw_downloads', '$version_raw_downloads_month', '$version_raw_downloads_week', '$version_raw_downloads_day', '$today', '$version_tags');"
    echo "Updated $owner/$package/$version_id"
}
