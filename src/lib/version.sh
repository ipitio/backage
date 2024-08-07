#!/bin/bash
# shellcheck disable=SC1091,SC2015,SC2154

source lib/util.sh

save_version() {
    check_limit || return $?
    [ -n "$1" ] || return
    local id
    local name
    local tags
    local versions_json
    id=$(_jq "$1" '.id')
    [ -f "${table_version_name}"_already_updated ] && grep -q "^$id$" "${table_version_name}"_already_updated && return || :
    name=$(_jq "$1" '.name')
    [[ "$id" =~ ^[0-9]+$ && "$name" != "latest" ]] || return
    tags=$(_jq "$1" '.. | try .tags | join(",")')
    [ -n "$tags" ] || tags=$(_jq "$1" '.. | try .tags')
    versions_json=$(get_BKG BKG_VERSIONS_JSON_"${owner}_${package}")
    [ -n "$versions_json" ] && jq -e . <<<"$versions_json" &>/dev/null || versions_json="[]"

    if jq -e ".[] | select(.id == \"$id\")" <<<"$versions_json" &>/dev/null; then
        # replace name and tags if the version is already in the versions_json
        versions_json=$(jq -c "map(if .id == \"$id\" then . + {\"name\":\"$name\",\"tags\":\"$tags\"} else . end)" <<<"$versions_json")
    else
        versions_json=$(jq -c ". + [{\"id\":\"$id\",\"name\":\"$name\",\"tags\":\"$tags\"}]" <<<"$versions_json")
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
            "https://api.github.com/$owner_type/$owner/packages/$package_type/$package/versions?per_page=100&page=$1")
        (($? != 3)) || return 3
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
    local version_size
    local version_id
    local manifest
    local sep
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
    fi

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

    sqlite3 "$BKG_INDEX_DB" "insert or replace into '$table_version_name' (id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('$version_id', '$version_name', '$version_size', '$version_raw_downloads', '$version_raw_downloads_month', '$version_raw_downloads_week', '$version_raw_downloads_day', '$BKG_TODAY', '$version_tags');"
    echo "Updated $owner/$package/$version_id"
}

refresh_version() {
    check_limit 21500 || return $?
    [ -n "$1" ] || return
    IFS='|' read -r vid vname vsize vdownloads vdownloads_month vdownloads_week vdownloads_day vdate vtags <<<"$1"
    [[ "$vid" =~ ^[0-9]+$ ]] || return
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
    echo "Refreshed $owner/$package/$vid"
}
