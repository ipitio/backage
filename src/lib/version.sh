#!/bin/bash
# shellcheck disable=SC1091,SC2015,SC2154

source lib/util.sh

version_stage_cleanup() {
    [ -n "${VERSION_STAGE_DIR:-}" ] || return 0
    [ -d "$VERSION_STAGE_DIR" ] || return 0
    rm -rf "$VERSION_STAGE_DIR"
    unset VERSION_STAGE_DIR
}

version_stage_reset() {
    local write_legacy=${VERSION_WRITE_LEGACY_TABLE:-}

    version_stage_cleanup
    VERSION_STAGE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/bkg-version-stage.XXXXXX") || return 1
    if [ -z "$write_legacy" ]; then
        version_legacy_table_exists && write_legacy=true || write_legacy=false
    fi
    jq -nc \
        --arg owner_id "${owner_id:-}" \
        --arg owner_type "${owner_type:-}" \
        --arg package_type "${package_type:-}" \
        --arg owner "${owner:-}" \
        --arg repo "${repo:-}" \
        --arg package "${package:-}" \
        --arg legacy_table "${table_version_name:-}" \
        --argjson write_legacy "$write_legacy" \
        '{
            owner_id: $owner_id,
            owner_type: $owner_type,
            package_type: $package_type,
            owner: $owner,
            repo: $repo,
            package: $package,
            legacy_table: $legacy_table,
            write_legacy: $write_legacy
        }' >"$VERSION_STAGE_DIR/manifest.json" || {
        version_stage_cleanup
        return 1
    }
}

version_stage_row_file() {
    [ -n "${VERSION_STAGE_DIR:-}" ] || return 1
    [ -d "$VERSION_STAGE_DIR" ] || return 1
    mktemp "$VERSION_STAGE_DIR/row.XXXXXX.json"
}

version_normalized_package_filter_sql() {
    printf 'owner_id = %s and owner_type = %s and package_type = %s and owner = %s and repo = %s and package = %s' \
        "$(sqlite_quote_literal "${owner_id:-}")" \
        "$(sqlite_quote_literal "${owner_type:-}")" \
        "$(sqlite_quote_literal "${package_type:-}")" \
        "$(sqlite_quote_literal "${owner:-}")" \
        "$(sqlite_quote_literal "${repo:-}")" \
        "$(sqlite_quote_literal "${package:-}")"
}

version_normalized_rows_available() {
    local batch_first_started=${1:-0000-00-00}
    local versions_table_sql
    local normalized_filter_sql

    versions_table_sql=$(sqlite_quote_identifier "$BKG_INDEX_TBL_VER")
    normalized_filter_sql=$(version_normalized_package_filter_sql)
    [ "$(sqlite3 "$BKG_INDEX_DB" "select 1 from $versions_table_sql where $normalized_filter_sql and date >= $(sqlite_quote_literal "$batch_first_started") limit 1;" 2>/dev/null || :)" = "1" ]
}

version_legacy_table_exists() {
    [ -n "${table_version_name:-}" ] || return 1
    [ "$(sqlite3 "$BKG_INDEX_DB" "select 1 from sqlite_master where type='table' and name=$(sqlite_quote_literal "$table_version_name") limit 1;" 2>/dev/null || :)" = "1" ]
}

version_select_source_sql() {
    local batch_first_started=${1:-0000-00-00}

    VERSION_SOURCE_TABLE_SQL=$(sqlite_quote_identifier "$BKG_INDEX_TBL_VER")
    VERSION_SOURCE_WHERE_SQL="$(version_normalized_package_filter_sql) and date >= $(sqlite_quote_literal "$batch_first_started")"

    if version_normalized_rows_available "$batch_first_started"; then
        return 0
    fi

    if version_legacy_table_exists; then
        VERSION_SOURCE_TABLE_SQL=$(sqlite_quote_identifier "$table_version_name")
        VERSION_SOURCE_WHERE_SQL="date >= $(sqlite_quote_literal "$batch_first_started")"
    fi
}

version_drop_legacy_table_if_replaced() {
    local batch_first_started=${1:-0000-00-00}

    version_legacy_table_exists || return 0
    bkg_python database cleanup-legacy-package \
        "${owner_id:-}" \
        "${owner_type:-}" \
        "${package_type:-}" \
        "${owner:-}" \
        "${repo:-}" \
        "${package:-}" \
        "$table_version_name" \
        "$batch_first_started" >/dev/null
}

version_stage_queue_row() {
    [ -n "$package" ] || return
    [ -n "${VERSION_STAGE_DIR:-}" ] || {
        echo "Version stage directory missing for $owner/$package" >&2
        return 1
    }

    local row_file

    row_file=$(version_stage_row_file) || return 1
    jq -nc \
        --arg id "$1" \
        --arg name "$2" \
        --argjson size "$3" \
        --argjson downloads "$4" \
        --argjson downloads_month "$5" \
        --argjson downloads_week "$6" \
        --argjson downloads_day "$7" \
        --arg date "$8" \
        --arg tags "$9" \
        '{
            id: $id,
            name: $name,
            size: $size,
            downloads: $downloads,
            downloads_month: $downloads_month,
            downloads_week: $downloads_week,
            downloads_day: $downloads_day,
            date: $date,
            tags: $tags
        }' >"$row_file"
}

version_flush_staged_rows() {
    [ -n "${VERSION_STAGE_DIR:-}" ] || return 0
    [ -d "$VERSION_STAGE_DIR" ] || return 0
    local -a row_files=()

    mapfile -t row_files < <(find "$VERSION_STAGE_DIR" -maxdepth 1 -type f -name 'row.*.json' | sort)
    ((${#row_files[@]} > 0)) || return 0
    bkg_python database flush-version-stage "$VERSION_STAGE_DIR"
}

legacy_version_build_array_json() {
    [ -n "$package" ] || return
    local newest_version_id="${1:-}"
    local latest_version_id="${2:-}"
    local version_limit=${3:--1}
    local version_since_date=${4:-}
    local batch_first_started
    local version_rows_sql
    local newest_version_id_sql
    local latest_version_id_sql

    [[ "$version_limit" =~ ^-?[0-9]+$ ]] || version_limit=-1
    batch_first_started=$(current_batch_first_started)
    [ -z "$version_since_date" ] || batch_first_started=$version_since_date
    [ -n "$batch_first_started" ] || batch_first_started="0000-00-00"
    version_select_source_sql "$batch_first_started"

    if ((version_limit < 0)); then
        version_rows_sql="select id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags from $VERSION_SOURCE_TABLE_SQL where $VERSION_SOURCE_WHERE_SQL;"
    else
        newest_version_id_sql=$(sqlite_quote_literal "$newest_version_id")
        latest_version_id_sql=$(sqlite_quote_literal "$latest_version_id")
        version_rows_sql="
            with version_candidates as (
                select
                    id,
                    name,
                    size,
                    downloads,
                    downloads_month,
                    downloads_week,
                    downloads_day,
                    date,
                    tags,
                    case when id != '' and id not glob '*[^0-9]*' then cast(id as integer) end as numeric_id,
                    row_number() over (
                        partition by id
                        order by date desc
                    ) as version_date_rank
                from $VERSION_SOURCE_TABLE_SQL
                where $VERSION_SOURCE_WHERE_SQL
            ),
            version_rows as (
                select
                    id,
                    name,
                    size,
                    downloads,
                    downloads_month,
                    downloads_week,
                    downloads_day,
                    date,
                    tags,
                    numeric_id
                from version_candidates
                where version_date_rank = 1
            ),
            ranked_versions as (
                select
                    id,
                    name,
                    size,
                    downloads,
                    downloads_month,
                    downloads_week,
                    downloads_day,
                    date,
                    tags,
                    row_number() over (
                        order by
                            case when numeric_id is null then 1 else 0 end desc,
                            coalesce(numeric_id, 0) desc,
                            id desc
                    ) as tail_rank,
                    case
                        when id = $newest_version_id_sql
                          or id = $latest_version_id_sql
                        then 1
                        else 0
                    end as mandatory
                from version_rows
            )
            select id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags
            from ranked_versions
            where mandatory = 1
               or ($version_limit > 0 and tail_rank <= $version_limit);
        "
    fi

    sqlite3 -json "$BKG_INDEX_DB" "$version_rows_sql" | jq -c --arg newest "$newest_version_id" --arg latest "$latest_version_id" --argjson version_limit "$version_limit" '
        def human_units($units; $spaced):
            . as $value
            | (if type == "number" then . else (tonumber? // .) end) as $n
            | if ($n | type) != "number" then
                ($value | tostring)
              else
                reduce range(0; ($units | length) - 1) as $i ({v: $n, s: 0};
                    if .v > 999.9 and .s < (($units | length) - 1) then
                        {v: (.v / 1000), s: (.s + 1)}
                    else
                        .
                    end
                )
                | (((.v * 10) | floor) / 10 | tostring) as $formatted
                | if ($units[.s] == "") then
                    $formatted
                  elif $spaced then
                    $formatted + " " + $units[.s]
                  else
                    $formatted + $units[.s]
                  end
              end;
        def human_metric: human_units(["", "k", "M", "B", "T", "P", "E", "Z", "Y"]; false);
        def human_size: human_units(["", "kB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB"]; true);
        def sort_id_key:
            [
                (if (.id | tonumber?) != null then 0 else 1 end),
                ((.id | tonumber?) // 0),
                (.id | tostring)
            ];

        group_by(.id)
        | map(max_by(.date))
        | sort_by(sort_id_key)
        | map(
            (.id | tostring) as $id
            | (.size | tonumber? // -1) as $size
            | (.downloads | tonumber? // -1) as $downloads
            | (.downloads_month | tonumber? // -1) as $downloads_month
            | (.downloads_week | tonumber? // -1) as $downloads_week
            | (.downloads_day | tonumber? // -1) as $downloads_day
            | {
                id: (.id | tonumber? // .id),
                name: .name,
                date: .date,
                newest: ($id == $newest),
                latest: ($id == $latest),
                size: ($size | human_size),
                downloads: ($downloads | human_metric),
                downloads_month: ($downloads_month | human_metric),
                downloads_week: ($downloads_week | human_metric),
                downloads_day: ($downloads_day | human_metric),
                raw_size: $size,
                raw_downloads: $downloads,
                raw_downloads_month: $downloads_month,
                raw_downloads_week: $downloads_week,
                raw_downloads_day: $downloads_day,
                tags: (
                    if .tags == null or (.tags | tostring) == "" then
                        []
                    else
                        (.tags | tostring | split(",") | map(gsub("^[[:space:]]+|[[:space:]]+$"; "")) | map(select(length > 0)))
                    end
                )
            }
        )
        | if $version_limit < 0 then
            .
          else
            (
                [ .[] | select(.latest == true or .newest == true) ]
                + (if $version_limit == 0 then [] else (sort_by(sort_id_key) | .[-$version_limit:]) end)
            )
            | unique_by(.id | tostring)
            | sort_by(sort_id_key)
          end
    '
}

version_build_array_json() {
    [ -n "$package" ] || return
    local version_limit=${3:--1}
    local version_since_date=${4:-}

    [[ "$version_limit" =~ ^-?[0-9]+$ ]] || version_limit=-1
    [ -n "$version_since_date" ] || version_since_date=$(current_batch_first_started)
    [ -n "$version_since_date" ] || version_since_date="0000-00-00"
    bkg_python render versions \
        "$owner_id" "$owner_type" "$package_type" "$owner" "$repo" "$package" \
        "$table_version_name" "$version_since_date" "$version_limit"
}

version_parse_page_html() {
    [ -n "$1" ] || return
    bkg_python version parse-page-html \
        "$owner_type" "$owner" "$repo" "$package_type" "$package" <<<"$1"
}

version_page_from_html() {
    [ -n "$1" ] || return
    [ -n "$package" ] || return
    local html

    html=$(curl "https://github.com/$owner/$repo/pkgs/$package_type/$package/versions?page=$1")
    (($? != 3)) || return 3
    version_parse_page_html "$html"
}

page_version() {
    check_limit || return $?
    [ -n "$1" ] || return
    [ -n "$package" ] || return
    local versions_json_more="[]"

    VERSION_PAGE_JSON="[]"
    VERSION_PAGE_COUNT=0

    if [ -n "$GITHUB_TOKEN" ]; then
        echo "Starting $owner/$package page $1..."
        versions_json_more=$(query_api "$owner_type/$owner/packages/$package_type/$package/versions?per_page=30&page=$1")
        (($? != 3)) || return 3
    fi

    if ! jq -e '.[].id' <<<"$versions_json_more" &>/dev/null; then
        (($1 > 1)) || echo "Falling back to HTML for $owner/$package..."
        versions_json_more=$(version_page_from_html "$1")
        (($? != 3)) || return 3
    fi

    jq -e '.[].id' <<<"$versions_json_more" &>/dev/null || return 2
    VERSION_PAGE_JSON=$(jq -c '.[0:30]' <<<"$versions_json_more")
    VERSION_PAGE_COUNT=$(jq 'length' <<<"$VERSION_PAGE_JSON")
    echo "Started $owner/$package page $1"
    ((VERSION_PAGE_COUNT >= 30)) || return 2
}

version_extract_tags() {
    [ -n "$1" ] || return
    local version_tags

    version_tags=$(_jq "$1" '.. | .tags? // empty | if type == "array" then join(",") else . end' | paste -sd, -)
    [[ "$version_tags" != "[]" && "$version_tags" != '"[]"' ]] || version_tags=""
    echo "$version_tags"
}

version_merge_tags() {
    [ -n "$1$2" ] || return
    printf '%s\n%s\n' "${1//,/$'\n'}" "${2//,/$'\n'}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | sed '/^$/d' | awk '!seen[$0]++' | paste -sd, -
}

version_reset_pipeline() {
    local max_tag_pages=${1:-3}

    unset VERSION_TAG_CACHE VERSION_SOURCE_LINES VERSION_SUBMITTED VERSION_PROVISIONAL_IDS VERSION_PAGE_IDS VERSION_TAGGED_IDS VERSION_TAGGED_IDS_SEEN
    declare -gA VERSION_TAG_CACHE=()
    declare -gA VERSION_SOURCE_LINES=()
    declare -gA VERSION_SUBMITTED=()
    declare -ga VERSION_PROVISIONAL_IDS=()
    declare -ga VERSION_PAGE_IDS=()
    declare -ga VERSION_TAGGED_IDS=()
    declare -gA VERSION_TAGGED_IDS_SEEN=()
    VERSION_PAGE_JSON="[]"
    VERSION_PAGE_COUNT=0
    VERSION_TAG_CACHE_MAX_PAGES=$max_tag_pages
    VERSION_TAG_CACHE_PAGES_FETCHED=0
    VERSION_TAG_CACHE_EXHAUSTED=false
}

version_has_unresolved_ids() {
    [ -n "$1" ] || return 1
    $VERSION_TAG_CACHE_EXHAUSTED && return 1

    while IFS= read -r version_id; do
        [ -n "$version_id" ] || continue
        [ -n "${VERSION_TAG_CACHE[$version_id]+x}" ] || return 0
    done <<<"$1"

    return 1
}

version_load_tag_cache_page() {
    [ -n "$1" ] || return
    [ -n "$package" ] || return
    local html
    local tag_link_count=0
    local tagged_versions_json="[]"
    local version_line
    local version_id

    html=$(curl "https://github.com/$owner/$repo/pkgs/$package_type/$package/versions?filters%5Bversion_type%5D=tagged&page=$1")
    (($? != 3)) || return 3
    tag_link_count=$(grep -Po '\?tag=' <<<"$html" | wc -l)

    tagged_versions_json=$(version_parse_page_html "$html")
    (($? != 3)) || return 3

    while IFS= read -r version_line; do
        [ -n "$version_line" ] || continue
        version_id=$(_jq "$version_line" '.id')
        [[ "$version_id" =~ ^[0-9]+$ ]] || version_id=-1
        version_cache_candidate "$version_line"

        if [ -z "${VERSION_TAGGED_IDS_SEEN[$version_id]+x}" ]; then
            VERSION_TAGGED_IDS+=("$version_id")
            VERSION_TAGGED_IDS_SEEN["$version_id"]=1
        fi
    done < <(jq -r '.[] | @base64' <<<"$tagged_versions_json")

    ((VERSION_TAG_CACHE_PAGES_FETCHED++))
    ((tag_link_count >= 30)) || VERSION_TAG_CACHE_EXHAUSTED=true
}

version_extend_tag_cache() {
    [ -n "$1" ] || return
    local watched_ids=$1
    local requested_pages=${2:-0}
    local remaining_pages=0

    ((requested_pages > 0)) || return
    remaining_pages=$((VERSION_TAG_CACHE_MAX_PAGES - VERSION_TAG_CACHE_PAGES_FETCHED))
    ((remaining_pages > 0)) || return
    ((requested_pages > remaining_pages)) && requested_pages=$remaining_pages

    while ((requested_pages > 0)) && version_has_unresolved_ids "$watched_ids"; do
        version_load_tag_cache_page "$((VERSION_TAG_CACHE_PAGES_FETCHED + 1))"
        (($? != 3)) || return 3
        ((requested_pages--))
    done
}

version_cache_candidate() {
    [ -n "$1" ] || return
    local version_id
    local version_tags

    version_id=$(_jq "$1" '.id')
    [[ "$version_id" =~ ^[0-9]+$ ]] || version_id=-1
    VERSION_SOURCE_LINES["$version_id"]="$1"
    version_tags=$(version_extract_tags "$1")
    [ -n "$version_tags" ] && VERSION_TAG_CACHE["$version_id"]=$(version_merge_tags "${VERSION_TAG_CACHE[$version_id]-}" "$version_tags")
}

version_hydrate_candidates() {
    [ -n "$1" ] || return
    local requested_tag_pages=${2:-0}
    local unresolved_ids=""
    local version_id
    local version_line
    local -a version_lines_a=()

    VERSION_PAGE_IDS=()
    mapfile -t version_lines_a <<<"$1"

    for version_line in "${version_lines_a[@]}"; do
        [ -n "$version_line" ] || continue
        version_id=$(_jq "$version_line" '.id')
        [[ "$version_id" =~ ^[0-9]+$ ]] || version_id=-1
        version_cache_candidate "$version_line"
        VERSION_PAGE_IDS+=("$version_id")

        if [ -z "${VERSION_TAG_CACHE[$version_id]-}" ]; then
            unresolved_ids+="$version_id"$'\n'
        fi
    done

    version_extend_tag_cache "$unresolved_ids" "$requested_tag_pages"
    (($? != 3)) || return 3
}

version_candidate_is_tagged() {
    [ -n "$1" ] || return
    [ -n "${VERSION_TAG_CACHE[$1]-}" ]
}

version_render_candidate() {
    [ -n "$1" ] || return
    [ -n "${VERSION_SOURCE_LINES[$1]+x}" ] || return

    echo "${VERSION_SOURCE_LINES[$1]}" | base64 --decode | jq -c --arg tags "${VERSION_TAG_CACHE[$1]-}" '{id, name, tags: $tags}' | base64 | tr -d '\n'
}

version_submit_candidate() {
    [ -n "$1" ] || return
    [ -n "${VERSION_SOURCE_LINES[$1]+x}" ] || return
    [ -z "${VERSION_SUBMITTED[$1]+x}" ] || return
    local candidate

    if [ -f "${table_version_name}"_already_updated ] && grep -Fxq "$1" "${table_version_name}"_already_updated; then
        VERSION_SUBMITTED["$1"]=1
        return
    fi

    candidate=$(version_render_candidate "$1")
    [ -n "$candidate" ] || return
    parallel_async_submit update_version "$candidate"
    (($? != 3)) || return 3
    VERSION_SUBMITTED["$1"]=1
}

version_store_fallback_candidate() {
    VERSION_SOURCE_LINES["-1"]=$(printf '%s' '{"id":-1,"name":"latest"}' | base64 | tr -d '\n')
    VERSION_TAG_CACHE["-1"]=""
}

version_oldest_submitted_numeric_id() {
    local oldest_id=""
    local version_id

    for version_id in "${!VERSION_SUBMITTED[@]}"; do
        [[ "$version_id" =~ ^[0-9]+$ ]] || continue

        if [ -z "$oldest_id" ] || ((version_id < oldest_id)); then
            oldest_id=$version_id
        fi
    done

    [ -z "$oldest_id" ] || printf '%s\n' "$oldest_id"
}

version_pop_provisional_slot() {
    ((${#VERSION_PROVISIONAL_IDS[@]} > 0)) || return 1
    unset 'VERSION_PROVISIONAL_IDS[0]'
    VERSION_PROVISIONAL_IDS=("${VERSION_PROVISIONAL_IDS[@]}")
}

version_submit_current_page_candidates() {
    local commit_count=${1:-0}
    local consume_provisional=${2:-false}
    local index
    local version_id

    for ((index = 0; index < ${#VERSION_PAGE_IDS[@]}; index++)); do
        if $consume_provisional && ((${#VERSION_PROVISIONAL_IDS[@]} == 0)); then
            break
        fi

        version_id=${VERSION_PAGE_IDS[index]}
        [ -z "${VERSION_SUBMITTED[$version_id]+x}" ] || continue

        if ((index < commit_count)) || version_candidate_is_tagged "$version_id"; then
            version_submit_candidate "$version_id"
            (($? != 3)) || return 3
            $consume_provisional && version_pop_provisional_slot || :
        fi
    done
}

version_collect_current_page_provisional() {
    local start_index=${1:-0}
    local index
    local version_id

    VERSION_PROVISIONAL_IDS=()

    for ((index = ${#VERSION_PAGE_IDS[@]} - 1; index >= start_index; index--)); do
        version_id=${VERSION_PAGE_IDS[index]}

        if [ -z "${VERSION_SUBMITTED[$version_id]+x}" ] && ! version_candidate_is_tagged "$version_id"; then
            VERSION_PROVISIONAL_IDS+=("$version_id")
        fi
    done
}

version_resolve_provisional_candidates() {
    local requested_tag_pages=${1:-0}
    local watched_ids
    local version_id
    local -a unresolved_ids=()

    ((${#VERSION_PROVISIONAL_IDS[@]} > 0)) || return 0
    watched_ids=$(printf '%s\n' "${VERSION_PROVISIONAL_IDS[@]}")
    version_extend_tag_cache "$watched_ids" "$requested_tag_pages"
    (($? != 3)) || return 3

    for version_id in "${VERSION_PROVISIONAL_IDS[@]}"; do
        if version_candidate_is_tagged "$version_id"; then
            version_submit_candidate "$version_id"
            (($? != 3)) || return 3
        else
            unresolved_ids+=("$version_id")
        fi
    done

    VERSION_PROVISIONAL_IDS=("${unresolved_ids[@]}")
}

version_promote_current_page_candidates() {
    local requested_tag_pages=${1:-0}
    local watched_ids

    version_submit_current_page_candidates 0 true
    (($? != 3)) || return 3
    ((${#VERSION_PROVISIONAL_IDS[@]} > 0)) || return 0

    watched_ids=$(printf '%s\n' "${VERSION_PAGE_IDS[@]}")
    version_extend_tag_cache "$watched_ids" "$requested_tag_pages"
    (($? != 3)) || return 3
    version_submit_current_page_candidates 0 true
    (($? != 3)) || return 3
}

version_extract_download_metric() {
    [ -n "$1" ] || return
    [ -n "$2" ] || return

    STAT_LABEL="$2" perl -0ne '
        my $label = quotemeta($ENV{STAT_LABEL} // q{});
        if (/$label<\/span>\s*<span[^>]*>([^<]+)/s) {
            print $1;
            exit 0;
        }
    ' <<<"$1" | tr -d '[:space:]' | fmtmetric_num
}

version_append_older_tagged_candidates() {
    local older_than_id=${1:-}
    local append_limit=${2:-30}
    local appended_count=0
    local version_id
    local loaded_page_count=0

    [[ "$older_than_id" =~ ^[0-9]+$ ]] || return 0
    ((append_limit > 0)) || return 0

    while :; do
        for version_id in "${VERSION_TAGGED_IDS[@]}"; do
            [[ "$version_id" =~ ^[0-9]+$ ]] || continue
            ((version_id < older_than_id)) || continue
            [ -z "${VERSION_SUBMITTED[$version_id]+x}" ] || continue

            version_submit_candidate "$version_id"
            (($? != 3)) || return 3
            ((appended_count++))
            ((appended_count < append_limit)) || return 0
        done

        $VERSION_TAG_CACHE_EXHAUSTED && return 0
        ((VERSION_TAG_CACHE_PAGES_FETCHED < VERSION_TAG_CACHE_MAX_PAGES)) || return 0
        loaded_page_count=$VERSION_TAG_CACHE_PAGES_FETCHED
        version_load_tag_cache_page "$((VERSION_TAG_CACHE_PAGES_FETCHED + 1))"
        (($? != 3)) || return 3
        ((VERSION_TAG_CACHE_PAGES_FETCHED > loaded_page_count)) || return 0
    done
}

update_version() {
    check_limit || return $?
    [ -n "$1" ] || return
    [ -n "$package" ] || return
    local stage_status=0
    local version_size=-1
    local version_raw_downloads=-1
    local version_raw_downloads_month=-1
    local version_raw_downloads_week=-1
    local version_raw_downloads_day=-1
    local version_html
    local version_name
    local version_tags
    local version_id
    local today
    today=$(date -u +%Y-%m-%d)
    version_id=$(_jq "$1" '.id')
    version_name=$(_jq "$1" '.name')
    version_tags=$(_jq "$1" '.tags')
    echo "Updating $owner/$package/$version_id..."
    version_html=$(curl "https://github.com/$owner/$repo/pkgs/$package_type/$package/$version_id")
    (($? != 3)) || return 3
    version_raw_downloads=$(version_extract_download_metric "$version_html" "Total downloads")
    version_raw_downloads_month=$(version_extract_download_metric "$version_html" "Last 30 days")
    version_raw_downloads_week=$(version_extract_download_metric "$version_html" "Last week")
    version_raw_downloads_day=$(version_extract_download_metric "$version_html" "Today")
    [[ "$version_raw_downloads" =~ ^[0-9]+$ ]] || version_raw_downloads=-1
    [[ "$version_raw_downloads_month" =~ ^[0-9]+$ ]] || version_raw_downloads_month=-1
    [[ "$version_raw_downloads_week" =~ ^[0-9]+$ ]] || version_raw_downloads_week=-1
    [[ "$version_raw_downloads_day" =~ ^[0-9]+$ ]] || version_raw_downloads_day=-1

    if [ "$package_type" = "container" ]; then
        # https://unix.stackexchange.com/q/550463, https://stackoverflow.com/q/45186440
        local manifest
        local manifest_ref
        local inspected_manifest
        manifest=$(awk -v RS='</pre>' '/<code.*?>/{gsub(/.*<code.*?>/, ""); print}' <<<"$version_html" | sed 's/&quot;/"/g')
        version_size=$(docker_manifest_size "$manifest" "$owner/$package/$version_id embedded manifest")
        [[ -n "$version_tags" ]] || version_tags=$(jq '.. | try ."org.opencontainers.image.version" | select(. != null and . != "")' <<<"$manifest" 2>/dev/null || :)

        if [[ ! "$version_size" =~ ^[0-9]+$ ]]; then
            manifest_ref="ghcr.io/$lower_owner/$lower_package$([[ "$version_name" =~ ^sha256:.+$ ]] && echo "@" || echo ":")$version_name"
            inspected_manifest=$(docker_manifest_inspect "$manifest_ref")
            (($? != 3)) || return 3
            version_size=$(docker_manifest_size "$inspected_manifest" "$manifest_ref inspected manifest")
        fi

        # last resort
        [[ "$version_size" =~ ^[0-9]+$ ]] || version_size=$(curl "https://ghcr-badge.egpl.dev/$owner/$package/size" | grep -oP '>\d+[^<]+' | tail -n1 | cut -c2- | fmtsize_num)
    else
        : # TODO: get size for other package types
    fi

    [[ "$version_size" =~ ^[0-9]+$ ]] || version_size=-1
    [[ "$version_tags" != "[]" && "$version_tags" != '"[]"' ]] || version_tags=""
    version_stage_queue_row "$version_id" "$version_name" "$version_size" "$version_raw_downloads" "$version_raw_downloads_month" "$version_raw_downloads_week" "$version_raw_downloads_day" "$today" "$version_tags" || stage_status=$?

    if ((stage_status != 0)); then
        ((stage_status != 3)) && echo "Failed to stage version row for $owner/$package/$version_id" >&2
        return "$stage_status"
    fi

    echo "Updated $owner/$package/$version_id"
}
