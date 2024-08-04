#!/bin/bash
# shellcheck disable=SC2015

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
    repo=$(grep -zoP '(?<=href="/'"$owner_type"'/'"$owner"'/packages/'"$package_type"'/package/'"$package_new"'")(.|\n)*?href="/'"$owner"'/[^"]+"' <<<"$html" | tr -d '\0' | grep -oP 'href="/'"$owner"'/[^"]+' | cut -d'/' -f3)
    package_type=${package_type%/}
    repo=${repo%/}
    [ -n "$repo" ] || return
    set_BKG_set BKG_PACKAGES_"$owner" "$package_type/$repo/$package_new"
    echo "Queued $owner/$package_new"
}

page_package() {
    check_limit || return $?
    [ -n "$1" ] || return
    local packages_lines
    local html
    echo "Starting $owner page $1..."
    [ "$owner_type" = "users" ] && html=$(curl "https://github.com/$owner?tab=packages&visibility=public&&per_page=100&page=$1") || html=$(curl "https://github.com/$owner_type/$owner/packages?visibility=public&per_page=100&page=$1")
    (($? != 3)) || return 3
    packages_lines=$(grep -zoP 'href="/'"$owner_type"'/'"$owner"'/packages/[^/]+/package/[^"]+"' <<<"$html" | tr -d '\0')

    if [ -z "$packages_lines" ]; then
        sed -i '/^'"$owner"'$/d' "$BKG_OWNERS"
        sed -i '/^'"$owner_id"'\/'"$owner"'$/d' "$BKG_OWNERS"
        return 2
    fi

    packages_lines=${packages_lines//href=/\\nhref=}
    packages_lines=${packages_lines//\\n/$'\n'} # replace \n with newline
    run_parallel save_package "$packages_lines" || return $?
    echo "Started $owner page $1"
    # if there are fewer than 100 lines, break
    [ "$(wc -l <<<"$packages_lines")" -eq 100 ] || return 2
}

update_package() {
    check_limit || return $?
    [ -n "$1" ] || return
    package_type=$(cut -d'/' -f1 <<<"$1")
    repo=$(cut -d'/' -f2 <<<"$1")
    package=$(cut -d'/' -f3 <<<"$1")
    package=${package%/}

    if grep -q "$owner/$repo/$package" "$BKG_OPTOUT"; then
        echo "$owner/$package was opted out!"
        sqlite3 "$BKG_INDEX_DB" "delete from '$BKG_INDEX_TBL_PKG' where owner_id='$owner_id' and package='$package';"
        sqlite3 "$BKG_INDEX_DB" "drop table if exists '${BKG_INDEX_TBL_VER}_${owner_type}_${package_type}_${owner}_${repo}_${package}';"
        return
    fi

    if [[ "$(sqlite3 "$BKG_INDEX_DB" "select exists(select 1 from '$BKG_INDEX_TBL_PKG' where owner_id='$owner_id' and package='$package') and date >= '$BKG_BATCH_FIRST_STARTED';")" == "1" && "$owner" != "arevindh" ]]; then
        echo "$owner/$package was already updated!"
        return
    fi

    local html
    local query
    local raw_downloads=-1
    local raw_downloads_month=-1
    local raw_downloads_week=-1
    local raw_downloads_day=-1
    local size=-1
    local versions_json=""

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
    (($? != 3)) || return 3
    [ -n "$(grep -Pzo 'Total downloads' <<<"$html" | tr -d '\0')" ] || return
    echo "Updating $owner/$package..."
    raw_downloads=$(grep -Pzo 'Total downloads[^"]*"\d*' <<<"$html" | grep -Pzo '\d*$' | tr -d '\0') # https://stackoverflow.com/a/74214537
    [[ "$raw_downloads" =~ ^[0-9]+$ ]] || raw_downloads=-1
    table_version_name="${BKG_INDEX_TBL_VER}_${owner_type}_${package_type}_${owner}_${repo}_${package}"

    for page in $(seq 1 100); do
        local pages_left=0
        set_BKG BKG_VERSIONS_JSON_"${owner}_${package}" "[]"

        if ((page == 1)) && [ -n "$(sqlite3 "$BKG_INDEX_DB" "select name from sqlite_master where type='table' and name='$table_version_name';")" ]; then
            run_parallel save_version "$(sqlite3 -json "$BKG_INDEX_DB" "select id, name, MAX(date), tags from '$table_version_name' group by id;" | jq -r '.[] | @base64')" || return $?
        fi

        page_version "$page"
        pages_left=$?
        ((pages_left != 3)) || return 3
        versions_json=$(get_BKG BKG_VERSIONS_JSON_"${owner}_${package}")
        jq -e . <<<"$versions_json" &>/dev/null || versions_json="[{\"id\":\"-1\",\"name\":\"latest\",\"tags\":\"\"}]"
        del_BKG BKG_VERSIONS_JSON_"${owner}_${package}"

        if [[ "$(jq -r '.[] | .id' <<<"$versions_json" | sort -u)" != "$(sqlite3 "$BKG_INDEX_DB" "select distinct id from '$table_version_name' where date >= '$BKG_BATCH_FIRST_STARTED';" | sort -u)" || "$owner" == "arevindh" ]]; then
            run_parallel update_version "$(jq -r '.[] | @base64' <<<"$versions_json")" || return $?
        fi

        ((pages_left != 2)) || break
    done

    # calculate the overall downloads and size
    if [ -n "$(sqlite3 "$BKG_INDEX_DB" "select name from sqlite_master where type='table' and name='$table_version_name';")" ]; then
        max_date=$(sqlite3 "$BKG_INDEX_DB" "select date from '$table_version_name' order by date desc limit 1;")
        query="select sum(downloads), sum(downloads_month), sum(downloads_week), sum(downloads_day) from '$table_version_name' where date='$max_date';"
        summed_raw_downloads=$(sqlite3 "$BKG_INDEX_DB" "$query" | cut -d'|' -f1)
        raw_downloads_month=$(sqlite3 "$BKG_INDEX_DB" "$query" | cut -d'|' -f2)
        raw_downloads_week=$(sqlite3 "$BKG_INDEX_DB" "$query" | cut -d'|' -f3)
        raw_downloads_day=$(sqlite3 "$BKG_INDEX_DB" "$query" | cut -d'|' -f4)
        [[ "$summed_raw_downloads" =~ ^[0-9]+$ ]] && ((summed_raw_downloads > raw_downloads)) && raw_downloads=$summed_raw_downloads || :
        size=$(sqlite3 "$BKG_INDEX_DB" "select size from '$table_version_name' where id='$(sqlite3 "$BKG_INDEX_DB" "select id from '$table_version_name' order by id desc limit 1;")' order by date desc limit 1;")
    fi

    sqlite3 "$BKG_INDEX_DB" "insert or replace into '$BKG_INDEX_TBL_PKG' (owner_id, owner_type, package_type, owner, repo, package, downloads, downloads_month, downloads_week, downloads_day, size, date) values ('$owner_id', '$owner_type', '$package_type', '$owner', '$repo', '$package', '$raw_downloads', '$raw_downloads_month', '$raw_downloads_week', '$raw_downloads_day', '$size', '$BKG_BATCH_FIRST_STARTED');"
    echo "Updated $owner/$package"
}

refresh_package() {
    check_limit 21500 || return $?
    [ -n "$1" ] || return
    local max_date
    local version_count
    local version_with_tag_count
    IFS='|' read -r owner_id owner_type package_type owner repo package downloads downloads_month downloads_week downloads_day size date tags <<<"$1"
    export tags
    max_date=$(sqlite3 "$BKG_INDEX_DB" "select date from '$BKG_INDEX_TBL_PKG' where owner_id='$owner_id' and package='$package' order by date desc limit 1;")
    [ "$date" = "$max_date" ] || return
    table_version_name="${BKG_INDEX_TBL_VER}_${owner_type}_${package_type}_${owner}_${repo}_${package}"
    max_date=$(sqlite3 "$BKG_INDEX_DB" "select date from '$table_version_name' order by date desc limit 1;")
    [[ ! "$max_date" < "$(date -d "$BKG_TODAY - 1 day" +%Y-%m-%d)" ]] || return
    echo "Refreshing $owner/$package..."
    json_file="$BKG_INDEX_DIR/$owner/$repo/$package.json"
    [ -d "$BKG_INDEX_DIR/$owner/$repo" ] || mkdir "$BKG_INDEX_DIR/$owner/$repo"
    version_count=0
    version_with_tag_count=0

    if [ -f "$json_file" ] && [ -s "$json_file" ] && jq -e . <<<"$(cat "$json_file")" &>/dev/null; then
        local another_date
        another_date=$(jq -r '.date' <"$json_file")

        if [[ "$another_date" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ && ! "$another_date" < "$BKG_BATCH_FIRST_STARTED" ]]; then
            return
        fi
    fi

    if [ -n "$(sqlite3 "$BKG_INDEX_DB" "select name from sqlite_master where type='table' and name='$table_version_name';")" ]; then
        version_count=$(sqlite3 "$BKG_INDEX_DB" "select count(distinct id) from '$table_version_name';")
        version_with_tag_count=$(sqlite3 "$BKG_INDEX_DB" "select count(distinct id) from '$table_version_name' where tags != '' and tags is not null;")
    fi

    echo "Refreshing $owner/$package..."
    echo "{
        \"owner_type\": \"$owner_type\",
        \"package_type\": \"$package_type\",
        \"owner_id\": \"$owner_id\",
        \"owner\": \"$owner\",
        \"repo\": \"$repo\",
        \"package\": \"$package\",
        \"date\": \"$max_date\",
        \"size\": \"$(numfmt_size <<<"${size:--1}")\",
        \"versions\": \"$(numfmt <<<"${version_count:--1}")\",
        \"tagged\": \"$(numfmt <<<"${version_with_tag_count:--1}")\",
        \"downloads\": \"$(numfmt <<<"${downloads:--1}")\",
        \"downloads_month\": \"$(numfmt <<<"${downloads_month:--1}")\",
        \"downloads_week\": \"$(numfmt <<<"${downloads_week:--1}")\",
        \"downloads_day\": \"$(numfmt <<<"${downloads_day:--1}")\",
        \"raw_size\": ${size:--1},
        \"raw_versions\": ${version_count:--1},
        \"raw_tagged\": ${version_with_tag_count:--1},
        \"raw_downloads\": ${downloads:--1},
        \"raw_downloads_month\": ${downloads_month:--1},
        \"raw_downloads_week\": ${downloads_week:--1},
        \"raw_downloads_day\": ${downloads_day:--1},
        \"version\":
    [" >"$json_file"

    # add the versions to index/"$owner".json
    if [ "${version_count:--1}" -gt 0 ]; then
        export version_newest_id
        version_newest_id=$(sqlite3 "$BKG_INDEX_DB" "select id from '$table_version_name' order by id desc limit 1;")
        rm -f "$json_file".*
        run_parallel refresh_version "$(sqlite3 "$BKG_INDEX_DB" "select * from '$table_version_name' where date >= '$max_date' group by id;")" || return $?
    fi

    if [[ -n $(find "$BKG_INDEX_DIR/$owner/$repo" -type f -name "$package.json.*") ]]; then
        cat "$json_file".* >>"$json_file"
        rm -f "$json_file".*
    else
        echo "{
            \"id\": -1,
            \"name\": \"latest\",
            \"date\": \"$date\",
            \"newest\": true,
            \"size\": \"$(numfmt_size <<<"${size:--1}")\",
            \"downloads\": \"$(numfmt <<<"${downloads:--1}")\",
            \"downloads_month\": \"$(numfmt <<<"${downloads_month:--1}")\",
            \"downloads_week\": \"$(numfmt <<<"${downloads_week:--1}")\",
            \"downloads_day\": \"$(numfmt <<<"${downloads_day:--1}")\",
            \"raw_size\": ${size:--1},
            \"raw_downloads\": ${downloads:--1},
            \"raw_downloads_month\": ${downloads_month:--1},
            \"raw_downloads_week\": ${downloads_week:--1},
            \"raw_downloads_day\": ${downloads_day:--1},
            \"tags\": [\"\"]
            }," >>"$json_file"
    fi

    # remove the last comma
    sed -i '$ s/,$//' "$json_file"
    echo "]}" >>"$json_file"
    jq -c . "$json_file" >"$json_file".tmp.json 2>/dev/null
    [ ! -f "$json_file".tmp.json ] || mv "$json_file".tmp.json "$json_file"
    local json_size
    json_size=$(stat -c %s "$json_file")

    # if the json is over 50MB, remove oldest versions from the packages with the most versions
    if jq -e . <<<"$(cat "$json_file")" &>/dev/null; then
        while [ "$json_size" -ge 50000000 ]; do
            jq -e 'map(.version | length > 0) | any' "$json_file" || break
            jq -c 'sort_by(.versions | tonumber) | reverse | map(select(.versions > 0)) | map(.version |= sort_by(.id | tonumber) | del(.version[0]))' "$json_file" >"$json_file".tmp.json
            mv "$json_file".tmp.json "$json_file"
            json_size=$(stat -c %s "$json_file")
        done
    elif [ "$json_size" -ge 100000000 ]; then
        rm -f "$json_file"
    fi

    echo "Refreshed $owner/$package"
}
