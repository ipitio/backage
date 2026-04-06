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
    ! awk -F'|' -v owner_id_key="$owner_id" -v owner_key="$owner" -v repo_key="$repo" -v package_key="$package_new" '$1 == owner_id_key && $2 == owner_key && $3 == repo_key && $4 == package_key { found = 1; exit } END { exit !found }' packages_already_updated || return
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
    local raw_downloads=-1
    local raw_downloads_month=-1
    local raw_downloads_week=-1
    local raw_downloads_day=-1
    local size=-1
    local version_row_count=-1
    local version_count=-1
    local version_with_tag_count=-1
    local version_newest_id=-1
    local latest_version=-1
    local version_array_file=""
    local version_array_status=0
    local package_stats_row=""
    local version_stats_row=""
    local package_max_downloads=-1
    local rank_stats_row=""
    local owner_rank
    local repo_rank
    local version_flush_status=0
    local package_write_status=0
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
        while IFS= read -r match; do
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
        done < <(grep "$owner" "$BKG_OPTOUT")
    elif [ "${fast_out:-false}" = "true" ]; then
        return
    fi

    # shellcheck disable=SC2034
    lower_package=$(perl -pe 's/%([0-9A-Fa-f]{2})/chr(hex($1))/eg' <<<"${package//%/%25}" | tr '[:upper:]' '[:lower:]')
    [ -d "$BKG_INDEX_DIR/$owner/$repo" ] || mkdir -p "$BKG_INDEX_DIR/$owner/$repo" 2>/dev/null
    cleanup_generated_json_sidecars "$BKG_INDEX_DIR/$owner/$repo"
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

    if ! awk -F'|' -v owner_id_key="$owner_id" -v owner_key="$owner" -v repo_key="$repo" -v package_key="$package" '$1 == owner_id_key && $2 == owner_key && $3 == repo_key && $4 == package_key { found = 1; exit } END { exit !found }' packages_already_updated; then
        html=$(curl "https://github.com/$owner/$repo/pkgs/$package_type/$package")
        (($? != 3)) || return 3
        [ -n "$(grep -Pzo 'Total downloads' <<<"$html" | tr -d '\0')" ] || return
        echo "Updating $owner/$package..."
        raw_downloads=$(grep -Pzo 'Total downloads[^"]*"\d*' <<<"$html" | grep -Pzo '\d*$' | tr -d '\0') # https://stackoverflow.com/a/74214537
        sqlite3 "$BKG_INDEX_DB" "select id from '$table_version_name' where date >= '$BKG_BATCH_FIRST_STARTED';" | sort -u >"${table_version_name}"_already_updated
        local max_version_pages=3
        local tag_cache_pages=3
        local page=1
        local pages_left=0
        local pipeline_status=0
        local update_versions_status=0
        local version_lines

        version_reset_pipeline "$tag_cache_pages"
        version_stage_reset
        pipeline_status=$?

        if ((pipeline_status == 3)); then
            rm -f "${table_version_name}"_already_updated
            return 3
        elif ((pipeline_status != 0)); then
            echo "Failed to initialize version staging for $owner/$package; using fallback data where needed" >&2
            update_versions_status=$pipeline_status
        else
            page_version "$page"
            pages_left=$?

            if ((pages_left == 3)); then
                pipeline_status=3
            fi

            version_lines=$(jq -r '.[] | @base64' <<<"$VERSION_PAGE_JSON")
            if ((pipeline_status != 3)) && [ -n "$version_lines" ]; then
                version_hydrate_candidates "$version_lines" 0
                pipeline_status=$?

                if ((pipeline_status != 3)); then
                    version_submit_current_page_candidates 5 false
                    pipeline_status=$?
                fi

                if ((pipeline_status != 3)); then
                    version_collect_current_page_provisional 5
                    version_resolve_provisional_candidates "$tag_cache_pages"
                    pipeline_status=$?
                fi
            fi

            while ((pipeline_status != 3)) && ((pages_left != 2)) && ((page < max_version_pages)) && ((${#VERSION_PROVISIONAL_IDS[@]} > 0)); do
                ((page++))
                page_version "$page"
                pages_left=$?

                if ((pages_left == 3)); then
                    pipeline_status=3
                    break
                fi

                version_lines=$(jq -r '.[] | @base64' <<<"$VERSION_PAGE_JSON")
                [ -n "$version_lines" ] || continue

                version_hydrate_candidates "$version_lines" 0
                pipeline_status=$?
                ((pipeline_status != 3)) || break
                version_promote_current_page_candidates "$tag_cache_pages"
                pipeline_status=$?
            done

            if ((pipeline_status != 3)) && ((${#VERSION_SOURCE_LINES[@]} == 0)); then
                version_store_fallback_candidate
                version_submit_candidate "-1"
                pipeline_status=$?
            fi

            if ((pipeline_status != 3)); then
                for version_id in "${VERSION_PROVISIONAL_IDS[@]}"; do
                    version_submit_candidate "$version_id"
                    pipeline_status=$?
                    ((pipeline_status != 3)) || break
                done
            fi
        fi

        parallel_async_wait
        update_versions_status=$?
        version_flush_staged_rows || version_flush_status=$?
        version_stage_cleanup

        rm -f "${table_version_name}"_already_updated
        ((pipeline_status != 3 && update_versions_status != 3 && version_flush_status != 3)) || return 3

        if ((update_versions_status != 0)); then
            echo "Version refresh had write errors for $owner/$package; using fallback data where needed" >&2
        fi

        if ((version_flush_status != 0)); then
            echo "Failed to flush staged version rows for $owner/$package; using fallback data where needed" >&2
        fi
    fi

    check_limit || return $?

    # calculate the overall downloads and size
    package_stats_row=$(sqlite3 "$BKG_INDEX_DB" "
        select
            coalesce((select size from '$table_version_name' where size > 1 order by CAST(id as integer) desc, date desc limit 1), -1),
            coalesce(sum(downloads), -1),
            coalesce(sum(downloads_month), -1),
            coalesce(sum(downloads_week), -1),
            coalesce(sum(downloads_day), -1),
            coalesce((select max(downloads) from '$BKG_INDEX_TBL_PKG' where owner_id='$owner_id' and package='$package'), -1)
        from '$table_version_name'
        where date >= '$BKG_BATCH_FIRST_STARTED';
    ")
    size=$(cut -d'|' -f1 <<<"$package_stats_row")
    summed_raw_downloads=$(cut -d'|' -f2 <<<"$package_stats_row")
    raw_downloads_month=$(cut -d'|' -f3 <<<"$package_stats_row")
    raw_downloads_week=$(cut -d'|' -f4 <<<"$package_stats_row")
    raw_downloads_day=$(cut -d'|' -f5 <<<"$package_stats_row")
    package_max_downloads=$(cut -d'|' -f6 <<<"$package_stats_row")
    [[ "$size" =~ ^[0-9]+$ ]] || size=-1
    [[ "$summed_raw_downloads" =~ ^[0-9]+$ ]] || summed_raw_downloads=-1
    [[ "$raw_downloads_month" =~ ^[0-9]+$ ]] || raw_downloads_month=-1
    [[ "$raw_downloads_week" =~ ^[0-9]+$ ]] || raw_downloads_week=-1
    [[ "$raw_downloads_day" =~ ^[0-9]+$ ]] || raw_downloads_day=-1
    [[ "$package_max_downloads" =~ ^[0-9]+$ ]] || package_max_downloads=-1
    [[ "$raw_downloads" =~ ^[0-9]+$ ]] || raw_downloads=$package_max_downloads
    [[ "$raw_downloads" =~ ^[0-9]+$ || "$raw_downloads" == "-1" ]] || return
    [[ "$summed_raw_downloads" =~ ^[0-9]+$ ]] && ((summed_raw_downloads > raw_downloads)) && raw_downloads=$summed_raw_downloads || :

    if ! awk -F'|' -v owner_id_key="$owner_id" -v owner_key="$owner" -v repo_key="$repo" -v package_key="$package" '$1 == owner_id_key && $2 == owner_key && $3 == repo_key && $4 == package_key { found = 1; exit } END { exit !found }' packages_already_updated || [ "$BKG_MODE" -eq 1 ]; then
        sqlite3 "$BKG_INDEX_DB" "insert or replace into '$BKG_INDEX_TBL_PKG' (owner_id, owner_type, package_type, owner, repo, package, downloads, downloads_month, downloads_week, downloads_day, size, date) values ('$owner_id', '$owner_type', '$package_type', '$owner', '$repo', '$package', '$raw_downloads', '$raw_downloads_month', '$raw_downloads_week', '$raw_downloads_day', '$size', '$(date -u +%Y-%m-%d)');" || package_write_status=$?

        if ((package_write_status == 0)); then
            echo "Updated $owner/$package, refreshing..."
        elif ((package_write_status != 3)); then
            echo "Failed to write package row for $owner/$package; continuing with existing package data" >&2
        fi
    fi

    version_stats_row=$(sqlite3 "$BKG_INDEX_DB" "
        with version_rows as (
            select
                id,
                tags,
                case when id regexp '^[0-9]+$' then CAST(id as integer) end as numeric_id,
                replace(replace(replace(replace(coalesce(tags, ''), ' ', ''), char(9), ''), char(10), ''), char(13), '') as compact_tags
            from '$table_version_name'
        ),
        stats as (
            select
                count(*) as version_row_count,
                count(distinct case when numeric_id is not null then id end) as version_count,
                count(distinct case when numeric_id is not null and tags is not null and tags != '' then id end) as version_with_tag_count,
                max(numeric_id) as version_newest_id,
                max(case when numeric_id is not null and tags is not null and tags != '' and (',' || compact_tags || ',') like '%,latest,%' then numeric_id end) as latest_exact,
                max(case when numeric_id is not null and tags is not null and tags != '' and instr(tags, '^') = 0 and instr(tags, '~') = 0 and instr(tags, '-') = 0 then numeric_id end) as latest_no_caret_tilde_hyphen,
                max(case when numeric_id is not null and tags is not null and tags != '' and instr(tags, '^') = 0 and instr(tags, '~') = 0 then numeric_id end) as latest_no_caret_tilde,
                max(case when numeric_id is not null and tags is not null and tags != '' and instr(tags, '^') = 0 then numeric_id end) as latest_no_caret,
                max(case when numeric_id is not null and tags is not null and tags != '' then numeric_id end) as latest_any_tagged
            from version_rows
        )
        select
            version_row_count,
            version_count,
            version_with_tag_count,
            coalesce(version_newest_id, ''),
            coalesce(latest_exact, latest_no_caret_tilde_hyphen, latest_no_caret_tilde, latest_no_caret, latest_any_tagged, ''),
            coalesce((select id from '$table_version_name' order by id desc limit 1), '')
        from stats;
    ")
    version_row_count=$(cut -d'|' -f1 <<<"$version_stats_row")
    version_count=$(cut -d'|' -f2 <<<"$version_stats_row")
    version_with_tag_count=$(cut -d'|' -f3 <<<"$version_stats_row")
	version_newest_id=$(cut -d'|' -f4 <<<"$version_stats_row")
	[[ "$version_row_count" =~ ^[0-9]+$ ]] || version_row_count=0
	[[ "$latest_version" =~ ^[0-9]+$ ]] || latest_version=$(cut -d'|' -f5 <<<"$version_stats_row")
	[[ "$latest_version" =~ ^[0-9]+$ ]] || latest_version=$(cut -d'|' -f6 <<<"$version_stats_row")
    [[ "$version_count" =~ ^[0-9]+$ ]] || version_count=0
    [[ "$version_with_tag_count" =~ ^[0-9]+$ ]] || version_with_tag_count=0
    [[ "$version_newest_id" =~ ^[0-9]+$ ]] || version_newest_id=-1
    [[ "$latest_version" =~ ^[0-9]+$ ]] || latest_version=-1
	version_array_file=$(mktemp) || return 1

	if ((version_row_count == 0)); then
		echo "No version rows available for $owner/$package; using package-level fallback data" >&2
		cat >"$version_array_file" <<EOF
[{
    "id": -1,
    "name": "latest",
    "date": "$(date -u +%Y-%m-%d)",
    "newest": true,
    "latest": true,
    "size": "$(numfmt_size <<<"$size")",
    "downloads": "$(numfmt <<<"$raw_downloads")",
    "downloads_month": "$(numfmt <<<"$raw_downloads_month")",
    "downloads_week": "$(numfmt <<<"$raw_downloads_week")",
    "downloads_day": "$(numfmt <<<"$raw_downloads_day")",
    "raw_size": $size,
    "raw_downloads": $raw_downloads,
    "raw_downloads_month": $raw_downloads_month,
    "raw_downloads_week": $raw_downloads_week,
    "raw_downloads_day": $raw_downloads_day,
    "tags": []
}]
EOF
	else
		version_build_array_json "$version_newest_id" "$latest_version" >"$version_array_file" || version_array_status=$?

		if ((version_array_status != 0)) || [ ! -s "$version_array_file" ]; then
			echo "Failed to build version array from database for $owner/$package; using package-level fallback data" >&2
			cat >"$version_array_file" <<EOF
[{
    "id": -1,
    "name": "latest",
    "date": "$(date -u +%Y-%m-%d)",
    "newest": true,
    "latest": true,
    "size": "$(numfmt_size <<<"$size")",
    "downloads": "$(numfmt <<<"$raw_downloads")",
    "downloads_month": "$(numfmt <<<"$raw_downloads_month")",
    "downloads_week": "$(numfmt <<<"$raw_downloads_week")",
    "downloads_day": "$(numfmt <<<"$raw_downloads_day")",
    "raw_size": $size,
    "raw_downloads": $raw_downloads,
    "raw_downloads_month": $raw_downloads_month,
    "raw_downloads_week": $raw_downloads_week,
    "raw_downloads_day": $raw_downloads_day,
    "tags": []
}]
EOF
		fi
	fi

    rank_stats_row=$(sqlite3 "$BKG_INDEX_DB" "
        with
        owner_latest as (
            select max(date) as latest_date
            from '$BKG_INDEX_TBL_PKG'
            where owner_id='$owner_id'
        ),
        repo_latest as (
            select max(date) as latest_date
            from '$BKG_INDEX_TBL_PKG'
            where owner_id='$owner_id' and repo='$repo'
        ),
        owner_ranked as (
            select package, rank() over (order by downloads desc) as rank
            from '$BKG_INDEX_TBL_PKG'
            where owner_id='$owner_id'
              and date = (select latest_date from owner_latest)
        ),
        repo_ranked as (
            select package, rank() over (order by downloads desc) as rank
            from '$BKG_INDEX_TBL_PKG'
            where owner_id='$owner_id'
              and repo='$repo'
              and date = (select latest_date from repo_latest)
        )
        select
            coalesce((select rank from owner_ranked where package='$package'), -1),
            coalesce((select rank from repo_ranked where package='$package'), -1);
    " || :)
    owner_rank=$(cut -d'|' -f1 <<<"$rank_stats_row")
    repo_rank=$(cut -d'|' -f2 <<<"$rank_stats_row")
    [[ "$owner_rank" =~ ^[0-9]+$ ]] || owner_rank=-1
    [[ "$repo_rank" =~ ^[0-9]+$ ]] || repo_rank=-1

    jq -cn \
        --arg owner_type "$owner_type" \
        --arg package_type "$package_type" \
        --arg owner "$owner" \
        --arg repo "$repo" \
        --arg package "$package" \
        --arg date "$(date -u +%Y-%m-%d)" \
        --arg size_fmt "$(numfmt_size <<<"$size")" \
        --arg versions_fmt "$(numfmt <<<"$version_count")" \
        --arg tagged_fmt "$(numfmt <<<"$version_with_tag_count")" \
        --arg owner_rank_fmt "$(numfmt <<<"$owner_rank")" \
        --arg repo_rank_fmt "$(numfmt <<<"$repo_rank")" \
        --arg downloads_fmt "$(numfmt <<<"$raw_downloads")" \
        --arg downloads_month_fmt "$(numfmt <<<"$raw_downloads_month")" \
        --arg downloads_week_fmt "$(numfmt <<<"$raw_downloads_week")" \
        --arg downloads_day_fmt "$(numfmt <<<"$raw_downloads_day")" \
        --argjson owner_id "$owner_id" \
        --argjson raw_size "$size" \
        --argjson raw_versions "$version_count" \
        --argjson raw_tagged "$version_with_tag_count" \
        --argjson raw_owner_rank "$owner_rank" \
        --argjson raw_repo_rank "$repo_rank" \
        --argjson raw_downloads "$raw_downloads" \
        --argjson raw_downloads_month "$raw_downloads_month" \
        --argjson raw_downloads_week "$raw_downloads_week" \
        --argjson raw_downloads_day "$raw_downloads_day" \
        --slurpfile version "$version_array_file" \
        '{
            owner_type: $owner_type,
            package_type: $package_type,
            owner_id: $owner_id,
            owner: $owner,
            repo: $repo,
            package: $package,
            date: $date,
            size: $size_fmt,
            versions: $versions_fmt,
            tagged: $tagged_fmt,
            owner_rank: $owner_rank_fmt,
            repo_rank: $repo_rank_fmt,
            downloads: $downloads_fmt,
            downloads_month: $downloads_month_fmt,
            downloads_week: $downloads_week_fmt,
            downloads_day: $downloads_day_fmt,
            raw_size: $raw_size,
            raw_versions: $raw_versions,
            raw_tagged: $raw_tagged,
            raw_owner_rank: $raw_owner_rank,
            raw_repo_rank: $raw_repo_rank,
            raw_downloads: $raw_downloads,
            raw_downloads_month: $raw_downloads_month,
            raw_downloads_week: $raw_downloads_week,
            raw_downloads_day: $raw_downloads_day,
            version: ($version[0] // [])
        }' >"$json_file".abs || echo "Failed to update $owner/$package with $size bytes and $raw_downloads downloads and $version_count versions and $version_with_tag_count tagged versions and $raw_downloads_month downloads this month and $raw_downloads_week downloads this week and $raw_downloads_day downloads today and $latest_version latest version and $version_newest_id newest version"
    rm -f "$version_array_file"
    [[ ! -f "$json_file".abs || ! -s "$json_file".abs ]] || mv "$json_file".abs "$json_file"
	bash lib/ytoxt.sh "$json_file"
    echo "Refreshed $owner/$package"
}
