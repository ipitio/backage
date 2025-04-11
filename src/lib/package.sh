#!/bin/bash
# shellcheck disable=SC1091,SC2015,SC2154

source lib/version.sh

save_package() {
    check_limit || return $?
    [ -n "$1" ] || return
    [ -n "$owner" ] || return
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
    ! set_BKG_set BKG_PACKAGES_"$owner" "$package_type/$repo/$package_new" || echo "Queued $owner/$package_new"
}

page_package() {
    [ -n "$1" ] || return
    [ -n "$owner" ] || return
    local packages_lines
    echo "Starting $owner page $1..."
    [ "$owner_type" = "users" ] && pkg_html=$(curl "https://github.com/$owner?tab=packages$([ "$BKG_MODE" -lt 2 ] && echo "&visibility=public" || { [ "$BKG_MODE" -eq 5 ] && echo "&visibility=private" || echo ""; })&per_page=100&page=$1") || pkg_html=$(curl "https://github.com/$owner_type/$owner/packages?per_page=100$([ "$BKG_MODE" -lt 2 ] && echo "&visibility=public" || { [ "$BKG_MODE" -eq 5 ] && echo "&visibility=private" || echo ""; })&page=$1")
    (($? != 3)) || return 3
    packages_lines=$(grep -zoP 'href="/'"$owner_type"'/'"$owner"'/packages/[^/]+/package/[^"]+"' <<<"$pkg_html" | tr -d '\0')
    [ -n "$packages_lines" ] || return 2
    packages_lines=${packages_lines//href=/\\nhref=}
    packages_lines=${packages_lines//\\n/$'\n'} # replace \n with newline
    run_parallel save_package "$packages_lines"
    (($1 > 1)) || grep -q href <<<"$packages_lines" || sed -i '/^\(.*\/\)*'"$owner"'$/d' "$BKG_OWNERS"
    (($? != 3)) || return 3
    echo "Started $owner page $1"
    [ "$(wc -l <<<"$packages_lines")" -gt 1 ] || return 2
}

optout_package() {
    echo "$2/$4 was opted out!"
    rm -rf "$BKG_INDEX_DIR/$2/$3/$4".*
    sqlite3 "$BKG_INDEX_DB" "delete from '$BKG_INDEX_TBL_PKG' where owner_id='$1' and package='$4';"
    sqlite3 "$BKG_INDEX_DB" "drop table if exists '$5';"
}

update_package() {
    check_limit || return $?
    [ -n "$1" ] || return
    [ -n "$owner" ] || return
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
    local latest_tags
    local owner_rank
    local repo_rank
    package_type=$(cut -d'/' -f1 <<<"$1")
    repo=$(cut -d'/' -f2 <<<"$1")
    package=$(cut -d'/' -f3 <<<"$1")
    package=${package%/}
    json_file="$BKG_INDEX_DIR/$owner/$repo/$package.json"
    table_version_name="${BKG_INDEX_TBL_VER}_${owner_type}_${package_type}_${owner}_${repo}_${package}"

    if grep -qP "^$owner(?=/$repo(?=/$package$|$)|$)" "$BKG_OPTOUT"; then
        optout_package "$owner_id" "$owner" "$repo" "$package" "$table_version_name"
        return
    elif grep -q "$owner" "$BKG_OPTOUT"; then
        grep "$owner" "$BKG_OPTOUT" | while IFS= read -r match; do
            local match_a
            local owner_out
            local repo_out
            local package_out
            mapfile -t match_a < <(perl -pe 's,/(?=/),\n,g' <<<"$match")
            owner_out=$([[ "$owner" == "${match_a[0]}" ]] || [[ "${match_a[0]}" =~ ^/ && "$owner" =~ $(sed 's/^\/\(.*\)/\1/' <<<"${match_a[0]}") ]] && echo true || echo false)
            repo_out=$( ((${#match_a[@]} < 2)) || [[ "$repo" == "${match_a[1]}" ]] || [[ "${match_a[1]}" =~ ^/ && "$repo" =~ $(sed 's/^\/\(.*\)/\1/' <<<"${match_a[1]}") ]] && echo true || echo false)
            package_out=$( ((${#match_a[@]} < 3)) || [[ "$package" == "${match_a[2]}" ]] || [[ "${match_a[2]}" =~ ^/ && "$package" =~ $(sed 's/^\/\(.*\)/\1/' <<<"${match_a[2]}") ]] && echo true || echo false)

            if $owner_out && $repo_out && $package_out; then
                optout_package "$owner_id" "$owner" "$repo" "$package" "$table_version_name"
                return
            fi
        done
    elif $fast_out; then
        return
    fi

    # shellcheck disable=SC2034
    lower_package=$(perl -pe 's/%([0-9A-Fa-f]{2})/chr(hex($1))/eg' <<<"${package//%/%25}" | tr '[:upper:]' '[:lower:]')
    [ -d "$BKG_INDEX_DIR/$owner/$repo" ] || mkdir "$BKG_INDEX_DIR/$owner/$repo" 2>/dev/null
    [ -d "$BKG_INDEX_DIR/$owner/$repo/$package.d" ] || mkdir "$BKG_INDEX_DIR/$owner/$repo/$package.d" 2>/dev/null
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

    if ! grep -q "^$owner_id|$owner|$repo|$package$" packages_already_updated || [ "$BKG_MODE" -eq 1 ]; then
        html=$(curl "https://github.com/$owner/$repo/pkgs/$package_type/$package")
        (($? != 3)) || return 3
        [ -n "$(grep -Pzo 'Total downloads' <<<"$html" | tr -d '\0')" ] || return
        echo "Updating $owner/$package..."
        raw_downloads=$(grep -Pzo 'Total downloads[^"]*"\d*' <<<"$html" | grep -Pzo '\d*$' | tr -d '\0') # https://stackoverflow.com/a/74214537
        sqlite3 "$BKG_INDEX_DB" "select id from '$table_version_name' where date >= '$BKG_BATCH_FIRST_STARTED';" | sort -u >"${table_version_name}"_already_updated
        local break_now=false

        for page in $(seq 0 5); do
            ((page > 0)) || continue
            local pages_left=0
            page_version "$page"
            pages_left=$?
            versions_json=$(jq -c -s '.' "$BKG_INDEX_DIR/$owner/$repo/$package".*.json 2>/dev/null)
            rm -f "$BKG_INDEX_DIR/$owner/$repo/$package".*.json
            ((pages_left != 3)) || return 3
            jq -e . <<<"$versions_json" &>/dev/null || versions_json="[{\"id\":\"-1\",\"name\":\"latest\",\"tags\":\"\"}]"
            ! jq -e 'length > 1' <<<"$versions_json" &>/dev/null || versions_json=$(jq -c 'map(select(.id >= 0))' <<<"$versions_json")
            [ -n "$latest_tags" ] || latest_tags=$(jq -r '.[].tags | select(. | split(",";"") | any(. | contains("latest")))' <<<"$versions_json")
            latest_tags=$(perl -pe 's/(?<!\\)"/\\"/g' <<<"$latest_tags")
            run_parallel update_version "$(jq -r '.[] | @base64' <<<"$versions_json")"
            (($? != 3)) || return 3
            ((pages_left != 2)) || break
            ! $break_now || break
            [ -z "$latest_tags" ] || break_now=true
        done

        rm -f "${table_version_name}"_already_updated
    fi

    # calculate the overall downloads and size
    size=$(sqlite3 "$BKG_INDEX_DB" "select size from '$table_version_name' where id in (select id from '$table_version_name' order by id desc limit 1) order by date desc limit 1;")
    raw_all=$(sqlite3 "$BKG_INDEX_DB" "select sum(downloads), sum(downloads_month), sum(downloads_week), sum(downloads_day) from '$table_version_name' where date in (select date from '$table_version_name' order by date desc limit 1);")
    summed_raw_downloads=$(cut -d'|' -f1 <<<"$raw_all")
    raw_downloads_month=$(cut -d'|' -f2 <<<"$raw_all")
    raw_downloads_week=$(cut -d'|' -f3 <<<"$raw_all")
    raw_downloads_day=$(cut -d'|' -f4 <<<"$raw_all")
    [[ "$size" =~ ^[0-9]+$ ]] || size=-1
    [[ "$summed_raw_downloads" =~ ^[0-9]+$ ]] || summed_raw_downloads=-1
    [[ "$raw_downloads_month" =~ ^[0-9]+$ ]] || raw_downloads_month=-1
    [[ "$raw_downloads_week" =~ ^[0-9]+$ ]] || raw_downloads_week=-1
    [[ "$raw_downloads_day" =~ ^[0-9]+$ ]] || raw_downloads_day=-1
    [[ "$raw_downloads" =~ ^[0-9]+$ ]] || raw_downloads=$(sqlite3 "$BKG_INDEX_DB" "select downloads from '$BKG_INDEX_TBL_PKG' where owner_id='$owner_id' and package='$package' and date in (select date from '$BKG_INDEX_TBL_PKG' where owner_id='$owner_id' and package='$package' order by date desc limit 1);")
    [[ "$raw_downloads" =~ ^[0-9]+$ || "$raw_downloads" == "-1" ]] || return
    [[ "$summed_raw_downloads" =~ ^[0-9]+$ ]] && ((summed_raw_downloads > raw_downloads)) && raw_downloads=$summed_raw_downloads || :

    if ! grep -q "^$owner_id|$owner|$repo|$package$" packages_already_updated || [ "$BKG_MODE" -eq 1 ]; then
        sqlite3 "$BKG_INDEX_DB" "insert or replace into '$BKG_INDEX_TBL_PKG' (owner_id, owner_type, package_type, owner, repo, package, downloads, downloads_month, downloads_week, downloads_day, size, date) values ('$owner_id', '$owner_type', '$package_type', '$owner', '$repo', '$package', '$raw_downloads', '$raw_downloads_month', '$raw_downloads_week', '$raw_downloads_day', '$size', '$BKG_BATCH_FIRST_STARTED');"
        echo "Updated $owner/$package, refreshing..."
    fi

    run_parallel save_version "$(sqlite3 -json "$BKG_INDEX_DB" "select * from '$table_version_name';" | jq -r 'group_by(.id)[] | max_by(.date) | @base64')"
    version_count=$(sqlite3 "$BKG_INDEX_DB" "select count(distinct id) from '$table_version_name' where id regexp '^[0-9]+$';")
    version_with_tag_count=$(sqlite3 "$BKG_INDEX_DB" "select count(distinct id) from '$table_version_name' where id regexp '^[0-9]+$' and tags != '' and tags is not null;")
    version_newest_id=$(find "$BKG_INDEX_DIR/$owner/$repo/$package.d" -type f -name "*.json" 2>/dev/null | grep -oP '\d+' | sort -n | tail -n1)
    [ -z "$latest_tags" ] || latest_version=$(sqlite3 "$BKG_INDEX_DB" "select id from '$table_version_name' where id regexp '^[0-9]+$' and tags like '%$latest_tags%' order by id desc limit 1;")
    [[ "$latest_version" =~ ^[0-9]+$ ]] || latest_version=$(sqlite3 "$BKG_INDEX_DB" "select id from '$table_version_name' where id regexp '^[0-9]+$' and tags != '' and tags is not null and tags regexp '^[^\^~-]+$' order by id desc limit 1;")
    [[ "$latest_version" =~ ^[0-9]+$ ]] || latest_version=$(sqlite3 "$BKG_INDEX_DB" "select id from '$table_version_name' where id regexp '^[0-9]+$' and tags != '' and tags is not null and tags regexp '^[^\^~]+$' order by id desc limit 1;")
    [[ "$latest_version" =~ ^[0-9]+$ ]] || latest_version=$(sqlite3 "$BKG_INDEX_DB" "select id from '$table_version_name' where id regexp '^[0-9]+$' and tags != '' and tags is not null and tags regexp '^[^\^]+$' order by id desc limit 1;")
    [[ "$latest_version" =~ ^[0-9]+$ ]] || latest_version=$(sqlite3 "$BKG_INDEX_DB" "select id from '$table_version_name' where id regexp '^[0-9]+$' and tags != '' and tags is not null order by id desc limit 1;")
    [[ "$version_count" =~ ^[0-9]+$ ]] || version_count=0
    [[ "$version_with_tag_count" =~ ^[0-9]+$ ]] || version_with_tag_count=0
    [[ "$version_newest_id" =~ ^[0-9]+$ ]] || version_newest_id=-1
    [[ "$latest_version" =~ ^[0-9]+$ ]] || latest_version=-1
    owner_rank=$(sqlite3 "$BKG_INDEX_DB" "select rank from (select package, rank () over (order by downloads desc) rank from '$BKG_INDEX_TBL_PKG' where owner_id='$owner_id' and date in (select date from '$BKG_INDEX_TBL_PKG' where owner_id='$owner_id' order by date desc limit 1)) where package='$package';")
    repo_rank=$(sqlite3 "$BKG_INDEX_DB" "select rank from (select package, rank () over (order by downloads desc) rank from '$BKG_INDEX_TBL_PKG' where owner_id='$owner_id' and repo='$repo' and date in (select date from '$BKG_INDEX_TBL_PKG' where owner_id='$owner_id' and repo='$repo' order by date desc limit 1)) where package='$package';")
    [[ "$owner_rank" =~ ^[0-9]+$ ]] || owner_rank=-1
    [[ "$repo_rank" =~ ^[0-9]+$ ]] || repo_rank=-1

    echo "{
        \"owner_type\": \"$owner_type\",
        \"package_type\": \"$package_type\",
        \"owner_id\": $owner_id,
        \"owner\": \"$owner\",
        \"repo\": \"$repo\",
        \"package\": \"$package\",
        \"date\": \"$(date -u +%Y-%m-%d)\",
        \"size\": \"$(numfmt_size <<<"$size")\",
        \"versions\": \"$(numfmt <<<"$version_count")\",
        \"tagged\": \"$(numfmt <<<"$version_with_tag_count")\",
        \"owner_rank\": \"$(numfmt <<<"$owner_rank")\",
        \"repo_rank\": \"$(numfmt <<<"$repo_rank")\",
        \"downloads\": \"$(numfmt <<<"$raw_downloads")\",
        \"downloads_month\": \"$(numfmt <<<"$raw_downloads_month")\",
        \"downloads_week\": \"$(numfmt <<<"$raw_downloads_week")\",
        \"downloads_day\": \"$(numfmt <<<"$raw_downloads_day")\",
        \"raw_size\": $size,
        \"raw_versions\": $version_count,
        \"raw_tagged\": $version_with_tag_count,
        \"raw_owner_rank\": $owner_rank,
        \"raw_repo_rank\": $repo_rank,
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
            \"tags\": []
        }]")
    }" | tr -d '\n' | jq -c . >"$json_file".abs || echo "Failed to update $owner/$package with $size bytes and $raw_downloads downloads and $version_count versions and $version_with_tag_count tagged versions and $raw_downloads_month downloads this month and $raw_downloads_week downloads this week and $raw_downloads_day downloads today and $latest_version latest version and $version_newest_id newest version"
    [[ ! -f "$json_file".abs || ! -s "$json_file".abs ]] || jq -c --arg newest "$version_newest_id" --arg latest "$latest_version" '.version |= map(if .id == ($newest | tonumber) then .newest = true else . end | if .id == ($latest | tonumber) then .latest = true else . end)' "$json_file".abs >"$json_file".rel
    [[ ! -f "$json_file".rel || ! -s "$json_file".rel ]] || mv "$json_file".rel "$json_file".abs
    [[ ! -f "$json_file".abs || ! -s "$json_file".abs ]] || mv "$json_file".abs "$json_file"

    # if the json is over 50MB, remove oldest versions from the packages with the most versions
    while [ -f "$json_file" ] && [ "$(stat -c %s "$json_file")" -ge 50000000 ]; do
        jq -e 'map(.version | length > 0) | any' "$json_file" || break
        jq -c 'sort_by(.versions | tonumber) | reverse | map(select(.versions > 0)) | map(.version |= sort_by(.id | tonumber) | del(.version[0]))' "$json_file" >"$json_file".tmp
        mv "$json_file".tmp "$json_file"
    done

    ytox "$json_file"
    rm -rf "$BKG_INDEX_DIR/$owner/$repo/$package.d"
    echo "Refreshed $owner/$package"
}
