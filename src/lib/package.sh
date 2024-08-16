#!/bin/bash
# shellcheck disable=SC1091,SC2015,SC2154

source lib/version.sh

save_package() {
    check_limit || return $?
    [ -n "$1" ] || return
    local package_new
    local package_type
    local repo
    package_new=$(cut -d'/' -f7 <<<"$1" | tr -d '"')
    package_new=${package_new%/}
    [ -n "$package_new" ] || return
    package_type=$(cut -d'/' -f5 <<<"$1")
    repo=$(grep -zoP '(?<=href="/'"$owner_type"'/'"$owner"'/packages/'"$package_type"'/package/'"$package_new"'")(.|\n)*?href="/'"$owner"'/[^"]+"' <<<"$pkg_html" | tr -d '\0' | grep -oP 'href="/'"$owner"'/[^"]+' | cut -d'/' -f3)
    package_type=${package_type%/}
    repo=${repo%/}
    [ -n "$repo" ] || return

    if [ -f packages_already_updated ] && grep -q "$id" packages_already_updated && [ -d "$BKG_INDEX_DIR/$owner/$repo/$package.d" ]; then
        [[ "$owner" == "arevindh" && "$(get_BKG BKG_AUTO)" -eq 1 ]] || return
    fi

    ! set_BKG_set BKG_PACKAGES_"$owner" "$package_type/$repo/$package_new" || echo "Queued $owner/$package_new"
}

page_package() {
    check_limit || return $?
    [ -n "$1" ] || return
    local packages_lines
    echo "Starting $owner page $1..."
    [ "$owner_type" = "users" ] && pkg_html=$(curl "https://github.com/$owner?tab=packages&visibility=public&&per_page=100&page=$1") || pkg_html=$(curl "https://github.com/$owner_type/$owner/packages?visibility=public&per_page=100&page=$1")
    (($? != 3)) || return 3
    packages_lines=$(grep -zoP 'href="/'"$owner_type"'/'"$owner"'/packages/[^/]+/package/[^"]+"' <<<"$pkg_html" | tr -d '\0')

    if [ -z "$packages_lines" ]; then
        sed -i '/^'"$owner"'$/d' "$BKG_OWNERS"
        sed -i '/^'"$owner_id"'\/'"$owner"'$/d' "$BKG_OWNERS"
        return 2
    fi

    packages_lines=${packages_lines//href=/\\nhref=}
    packages_lines=${packages_lines//\\n/$'\n'} # replace \n with newline
    run_parallel save_package "$packages_lines"
    (($? != 3)) || return 3
    echo "Started $owner page $1"
    # if there are fewer than 100 lines, break
    [ "$(wc -l <<<"$packages_lines")" -eq 100 ] || return 2
}

update_package() {
    check_limit || return $?
    [ -n "$1" ] || return
    local html
    local raw_all
    local raw_downloads=-1
    local raw_downloads_month=-1
    local raw_downloads_week=-1
    local raw_downloads_day=-1
    local size=-1
    local versions_json=""
    local version_count=-1
    local version_with_tag_count=-1
    local version_newest_id=-1
    local latest_version=-1
    export lower_owner
    export lower_package
    export version_ids
    package_type=$(cut -d'/' -f1 <<<"$1")
    repo=$(cut -d'/' -f2 <<<"$1")
    package=$(cut -d'/' -f3 <<<"$1")
    package=${package%/}
    json_file="$BKG_INDEX_DIR/$owner/$repo/$package.json"

    if grep -q "$owner/$repo/$package" "$BKG_OPTOUT"; then
        echo "$owner/$package was opted out!"
        rm -rf "$BKG_INDEX_DIR/$owner/$repo/$package".*
        sqlite3 "$BKG_INDEX_DB" "delete from '$BKG_INDEX_TBL_PKG' where owner_id='$owner_id' and package='$package';"
        sqlite3 "$BKG_INDEX_DB" "drop table if exists '${BKG_INDEX_TBL_VER}_${owner_type}_${package_type}_${owner}_${repo}_${package}';"
        return
    fi

    lower_package=$(perl -pe 's/%([0-9A-Fa-f]{2})/chr(hex($1))/eg' <<<"${package//%/%25}" | tr '[:upper:]' '[:lower:]')
    # scrape the package page for the total downloads
    [ -d "$BKG_INDEX_DIR/$owner/$repo" ] || mkdir "$BKG_INDEX_DIR/$owner/$repo"
    [ -d "$BKG_INDEX_DIR/$owner/$repo/$package.d" ] || mkdir "$BKG_INDEX_DIR/$owner/$repo/$package.d"
    html=$(curl "https://github.com/$owner/$repo/pkgs/$package_type/$package")
    (($? != 3)) || return 3
    [ -n "$(grep -Pzo 'Total downloads' <<<"$html" | tr -d '\0')" ] || return
    echo "Updating $owner/$package..."
    raw_downloads=$(grep -Pzo 'Total downloads[^"]*"\d*' <<<"$html" | grep -Pzo '\d*$' | tr -d '\0') # https://stackoverflow.com/a/74214537
    [[ "$raw_downloads" =~ ^[0-9]+$ ]] || raw_downloads=-1
    table_version_name="${BKG_INDEX_TBL_VER}_${owner_type}_${package_type}_${owner}_${repo}_${package}"
    sqlite3 "$BKG_INDEX_DB" "create table if not exists '$table_version_name' (
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
    sqlite3 "$BKG_INDEX_DB" "select id from '$table_version_name' where date >= '$BKG_BATCH_FIRST_STARTED';" | sort -u >"${table_version_name}"_already_updated
    version_ids=$(sqlite3 "$BKG_INDEX_DB" "select distinct id from '$table_version_name';")

    if [ "$(get_BKG BKG_AUTO)" = "0" ] || [ "$owner" = "arevindh" ]; then
        for page in $(seq 1 100); do
            local pages_left=0
            set_BKG BKG_VERSIONS_JSON_"${owner}_${package}" "[]"
            ((page != 1)) || run_parallel save_version "$(sqlite3 -json "$BKG_INDEX_DB" "select id, name, tags, max(date) from '$table_version_name' group by id;" | jq -r '.[] | @base64')"
            page_version "$page"
            pages_left=$?
            ((pages_left != 3)) || return 3
            versions_json=$(get_BKG BKG_VERSIONS_JSON_"${owner}_${package}")
            jq -e . <<<"$versions_json" &>/dev/null || versions_json="[{\"id\":\"-1\",\"name\":\"latest\",\"tags\":\"\"}]"
            del_BKG BKG_VERSIONS_JSON_"${owner}_${package}"
            run_parallel update_version "$(jq -r '.[] | @base64' <<<"$versions_json")"
            (($? != 3)) || return 3
            ((page != 1)) || version_newest_id=$(jq -r '.[].id' <<<"$versions_json" | sort -n | tail -n1)
            ((pages_left != 2)) || break
        done

        sqlite3 "$BKG_INDEX_DB" "insert or replace into '$BKG_INDEX_TBL_PKG' (owner_id, owner_type, package_type, owner, repo, package, downloads, downloads_month, downloads_week, downloads_day, size, date) values ('$owner_id', '$owner_type', '$package_type', '$owner', '$repo', '$package', '$raw_downloads', '$raw_downloads_month', '$raw_downloads_week', '$raw_downloads_day', '$size', '$BKG_BATCH_FIRST_STARTED');"
        echo "Updated $owner/$package"
    fi

    # calculate the overall downloads and size
    raw_all=$(sqlite3 "$BKG_INDEX_DB" "select sum(downloads), sum(downloads_month), sum(downloads_week), sum(downloads_day) from '$table_version_name' where date = (select date from '$table_version_name' order by date desc limit 1);")
    summed_raw_downloads=$(cut -d'|' -f1 <<<"$raw_all")
    raw_downloads_month=$(cut -d'|' -f2 <<<"$raw_all")
    raw_downloads_week=$(cut -d'|' -f3 <<<"$raw_all")
    raw_downloads_day=$(cut -d'|' -f4 <<<"$raw_all")
    [[ "$summed_raw_downloads" =~ ^[0-9]+$ ]] && ((summed_raw_downloads > raw_downloads)) && raw_downloads=$summed_raw_downloads || :
    size=$(sqlite3 "$BKG_INDEX_DB" "select size from '$table_version_name' where id='$(sqlite3 "$BKG_INDEX_DB" "select id from '$table_version_name' order by id desc limit 1;")' order by date desc limit 1;")
    version_count=$(sqlite3 "$BKG_INDEX_DB" "select count(distinct id) from '$table_version_name' where id regexp '^[0-9]+$';")
    version_with_tag_count=$(sqlite3 "$BKG_INDEX_DB" "select count(distinct id) from '$table_version_name' where id regexp '^[0-9]+$' and tags != '' and tags is not null;")
    ((version_with_tag_count <= 0)) || latest_version=$(sqlite3 "$BKG_INDEX_DB" "select id from '$table_version_name' where id regexp '^[0-9]+$' and tags != '' and tags is not null order by id desc limit 1;")
    run_parallel refresh_version "$(sqlite3 -json "$BKG_INDEX_DB" "select * from '$table_version_name';" | jq -r 'group_by(.id)[] | max_by(.date) | @base64')"
    echo "{
        \"owner_type\": \"$owner_type\",
        \"package_type\": \"$package_type\",
        \"owner_id\": \"$owner_id\",
        \"owner\": \"$owner\",
        \"repo\": \"$repo\",
        \"package\": \"$package\",
        \"date\": \"$(date -u +%Y-%m-%d)\",
        \"size\": \"$(numfmt_size <<<"$size")\",
        \"versions\": \"$(numfmt <<<"$version_count")\",
        \"tagged\": \"$(numfmt <<<"$version_with_tag_count")\",
        \"downloads\": \"$(numfmt <<<"$raw_downloads")\",
        \"downloads_month\": \"$(numfmt <<<"$raw_downloads_month")\",
        \"downloads_week\": \"$(numfmt <<<"$raw_downloads_week")\",
        \"downloads_day\": \"$(numfmt <<<"$raw_downloads_day")\",
        \"raw_size\": $size,
        \"raw_versions\": $version_count,
        \"raw_tagged\": $version_with_tag_count,
        \"raw_downloads\": $raw_downloads,
        \"raw_downloads_month\": $raw_downloads_month,
        \"raw_downloads_week\": $raw_downloads_week,
        \"raw_downloads_day\": $raw_downloads_day,
        \"version\": $([[ -n "$(find "$BKG_INDEX_DIR/$owner/$repo/$package.d" -type f -name "*.json" 2>/dev/null)" ]] && jq -s '.' "$BKG_INDEX_DIR/$owner/$repo/$package.d"/*.json || echo "[{
            \"id\": -1,
            \"name\": \"latest\",
            \"date\": \"$(date -u +%Y-%m-%d)\",
            \"newest\": true,
            \"latest\": true,
            \"size\": \"$(numfmt_size <<<"$size")\",
            \"downloads\": \"$(numfmt <<<"$raw_downloads")\",
            \"downloads_month\": \"$(numfmt <<<"$raw_downloads_month")\",
            \"downloads_week\": \"$(numfmt <<<"$raw_downloads_week")\",
            \"downloads_day\": \"$(numfmt <<<"$raw_downloads_day")\",
            \"raw_size\": $size,
            \"raw_downloads\": $raw_downloads,
            \"raw_downloads_month\": $raw_downloads_month,
            \"raw_downloads_week\": $raw_downloads_week,
            \"raw_downloads_day\": $raw_downloads_day,
            \"tags\": [\"\"]
        }]")
    }" | tr -d '\n' | jq -c . >"$json_file".tmp
    jq -c --arg newest "$version_newest_id" --arg latest "$latest_version" '.version |= map(if .id == ($newest | tonumber) then .newest = true else . end | if .id == ($latest | tonumber) then .latest = true else . end)' "$json_file".tmp >"$json_file"

    # if the json is over 50MB, remove oldest versions from the packages with the most versions
    while [ "$(stat -c %s "$json_file")" -ge 50000000 ]; do
        jq -e 'map(.version | length > 0) | any' "$json_file" || break
        jq -c 'sort_by(.versions | tonumber) | reverse | map(select(.versions > 0)) | map(.version |= sort_by(.id | tonumber) | del(.version[0]))' "$json_file" >"$json_file".tmp.json
        mv "$json_file".tmp.json "$json_file"
    done

    rm -f "${table_version_name}"_already_updated "${table_version_name}"_to_update all_"${table_version_name}"
    echo "Refreshed $owner/$package"
}
