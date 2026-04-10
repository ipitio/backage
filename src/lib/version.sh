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
    version_stage_cleanup
    VERSION_STAGE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/bkg-version-stage.XXXXXX") || return 1
}

version_stage_row_file() {
    [ -n "${VERSION_STAGE_DIR:-}" ] || return 1
    [ -d "$VERSION_STAGE_DIR" ] || return 1
    mktemp "$VERSION_STAGE_DIR/row.XXXXXX.sql"
}

version_stage_queue_row() {
    [ -n "$package" ] || return
    [ -n "${VERSION_STAGE_DIR:-}" ] || {
        echo "Version stage directory missing for $owner/$package" >&2
        return 1
    }

    local row_file
    local table_name_sql
    local version_id_sql
    local version_name_sql
    local version_tags_sql
    local version_date_sql

    row_file=$(version_stage_row_file) || return 1
    table_name_sql=$(sqlite_escape_identifier "$table_version_name")
    version_id_sql=$(sqlite_escape_literal "$1")
    version_name_sql=$(sqlite_escape_literal "$2")
    version_tags_sql=$(sqlite_escape_literal "$9")
    version_date_sql=$(sqlite_escape_literal "$8")

    cat >"$row_file" <<EOF
insert or replace into "$table_name_sql" (id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('$version_id_sql', '$version_name_sql', $3, $4, $5, $6, $7, '$version_date_sql', '$version_tags_sql');
EOF
}

version_flush_staged_rows() {
    [ -n "${VERSION_STAGE_DIR:-}" ] || return 0
    [ -d "$VERSION_STAGE_DIR" ] || return 0
    local sql_file
    local sql_statement
    local row_file
    local -a row_files=()

    mapfile -t row_files < <(find "$VERSION_STAGE_DIR" -maxdepth 1 -type f -name '*.sql' | sort)
    ((${#row_files[@]} > 0)) || return 0
    sql_file=$(mktemp) || return 1

    {
        printf 'BEGIN IMMEDIATE;\n'
        for row_file in "${row_files[@]}"; do
            cat "$row_file"
            printf '\n'
        done
        printf 'COMMIT;\n'
    } >"$sql_file"

    sql_statement=$(cat "$sql_file")
    rm -f "$sql_file"
    sqlite3 "$BKG_INDEX_DB" "$sql_statement"
}

version_build_array_json() {
    [ -n "$package" ] || return
    local newest_version_id="${1:-}"
    local latest_version_id="${2:-}"
    local batch_first_started="${BKG_BATCH_FIRST_STARTED:-0000-00-00}"

    sqlite3 -json "$BKG_INDEX_DB" "select * from '$table_version_name' where date >= '$batch_first_started';" | jq -c --arg newest "$newest_version_id" --arg latest "$latest_version_id" '
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
    '
}

version_parse_page_html() {
    [ -n "$1" ] || return
    VERSION_OWNER_PREFIX="$owner_type/$owner/packages/$package_type/$package" \
        VERSION_REPO_PREFIX="$owner/$repo/pkgs/$package_type/$package" \
        perl -0ne '
            sub decode_text {
                my ($value) = @_;
                $value //= q{};
                $value =~ s/&amp;/&/g;
                $value =~ s/&quot;/"/g;
                $value =~ s/&#39;/'"'"'/g;
                $value =~ s/&lt;/</g;
                $value =~ s/&gt;/>/g;
                $value =~ s/\+/ /g;
                $value =~ s/%([0-9A-Fa-f]{2})/chr(hex($1))/eg;
                return $value;
            }

            sub escape_json {
                my ($value) = @_;
                $value //= q{};
                $value =~ s/\\/\\\\/g;
                $value =~ s/"/\\"/g;
                $value =~ s/\n/\\n/g;
                return $value;
            }

            my $owner_prefix = quotemeta($ENV{VERSION_OWNER_PREFIX});
            my $repo_prefix = quotemeta($ENV{VERSION_REPO_PREFIX});
            my $prefix_pattern = qr/(?:$owner_prefix|$repo_prefix)/;

            while (/<li\b[^>]*class="Box-row"[^>]*>(.*?)<\/li>/sg) {
                my $block = $1;
                my ($version_id, $version_name);
                my %seen_tags;
                my @version_tags;

                while ($block =~ m{href="/$prefix_pattern/([0-9]+)\?tag=([^"&]+)}g) {
                    $version_id //= $1;
                    my $tag = decode_text($2);
                    next if $tag eq q{} || $seen_tags{$tag}++;
                    push @version_tags, $tag;
                }

                if (!$version_id && $block =~ m{href="/$prefix_pattern/([0-9]+)"}g) {
                    $version_id = $1;
                }

                next unless $version_id;

                if ($block =~ m{href="/$prefix_pattern/\Q$version_id\E"[^>]*>([^<]+)</a>}s) {
                    $version_name = decode_text($1);
                }

                if ((!defined $version_name || $version_name eq q{}) && $block =~ m{value="([^"]+)"}s) {
                    $version_name = decode_text($1);
                }

                if ((!defined $version_name || $version_name eq q{}) && $block =~ m{<span class="color-fg-muted">([^<]+)</span>}s) {
                    my $candidate = decode_text($1);
                    $version_name = $candidate if $candidate =~ /^(?:sha256:|[[:alnum:]][^[:space:]]*)/;
                }

                $version_name = $version_id unless defined $version_name && $version_name ne q{};

                my $tags_json = join q{,}, map { q{"} . escape_json($_) . q{"} } @version_tags;
                print qq[{"id":$version_id,"name":"] . escape_json($version_name) . qq[","tags":[$tags_json]}\n];
            }
        ' <<<"$1" | jq -cs '.'
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

    unset VERSION_TAG_CACHE VERSION_SOURCE_LINES VERSION_SUBMITTED VERSION_PROVISIONAL_IDS VERSION_PAGE_IDS
    declare -gA VERSION_TAG_CACHE=()
    declare -gA VERSION_SOURCE_LINES=()
    declare -gA VERSION_SUBMITTED=()
    declare -ga VERSION_PROVISIONAL_IDS=()
    declare -ga VERSION_PAGE_IDS=()
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

    html=$(curl "https://github.com/$owner/$repo/pkgs/$package_type/$package/versions?filters%5Bversion_type%5D=tagged&page=$1")
    (($? != 3)) || return 3
    tag_link_count=$(grep -Po '\?tag=' <<<"$html" | wc -l)

    while IFS='|' read -r version_id version_tags; do
        [ -n "$version_id" ] || continue
        VERSION_TAG_CACHE["$version_id"]=$(version_merge_tags "${VERSION_TAG_CACHE[$version_id]}" "$version_tags")
    done < <(VERSION_OWNER_PREFIX="$owner_type/$owner/packages/$package_type/$package" \
        VERSION_REPO_PREFIX="$owner/$repo/pkgs/$package_type/$package" \
        perl -0ne '
            sub decode_text {
                my ($value) = @_;
                $value //= q{};
                $value =~ s/\+/ /g;
                $value =~ s/%([0-9A-Fa-f]{2})/chr(hex($1))/eg;
                return $value;
            }

            my $owner_prefix = quotemeta($ENV{VERSION_OWNER_PREFIX});
            my $repo_prefix = quotemeta($ENV{VERSION_REPO_PREFIX});
            my $prefix_pattern = qr/(?:$owner_prefix|$repo_prefix)/;

            while (/href="\/$prefix_pattern\/([0-9]+)\?tag=([^"&]+)/g) {
                my $tag = decode_text($2);
                next if $tag eq q{} || $seen{$1}{$tag}++;
                push @{$tags{$1}}, $tag;
            }

            END {
                for my $id (keys %tags) {
                    print "$id|" . join(q{,}, @{$tags{$id}}) . "\n";
                }
            }
        ' <<<"$html")

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
    [ -n "$version_tags" ] && VERSION_TAG_CACHE["$version_id"]=$(version_merge_tags "${VERSION_TAG_CACHE[$version_id]}" "$version_tags")
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

        if [ -z "${VERSION_TAG_CACHE[$version_id]}" ]; then
            unresolved_ids+="$version_id"$'\n'
        fi
    done

    version_extend_tag_cache "$unresolved_ids" "$requested_tag_pages"
    (($? != 3)) || return 3
}

version_candidate_is_tagged() {
    [ -n "$1" ] || return
    [ -n "${VERSION_TAG_CACHE[$1]}" ]
}

version_render_candidate() {
    [ -n "$1" ] || return
    [ -n "${VERSION_SOURCE_LINES[$1]+x}" ] || return

    echo "${VERSION_SOURCE_LINES[$1]}" | base64 --decode | jq -c --arg tags "${VERSION_TAG_CACHE[$1]}" '{id, name, tags: $tags}' | base64 | tr -d '\n'
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
    version_raw_downloads=$(echo "$version_html" | grep -Pzo 'Total downloads<[^<]*<[^<]*' | grep -Pzo '(,|\d)*$' | tr -d '\0' | tr -d ',')

    if [[ "$version_raw_downloads" =~ ^[0-9]+$ ]]; then
        version_raw_downloads_month=$(grep -Pzo 'Last 30 days<[^<]*<[^<]*' <<<"$version_html" | grep -Pzo '(,|\d)*$' | tr -d '\0' | tr -d ',')
        version_raw_downloads_week=$(grep -Pzo 'Last week<[^<]*<[^<]*' <<<"$version_html" | grep -Pzo '(,|\d)*$' | tr -d '\0' | tr -d ',')
        version_raw_downloads_day=$(grep -Pzo 'Today<[^<]*<[^<]*' <<<"$version_html" | grep -Pzo '(,|\d)*$' | tr -d '\0' | tr -d ',')
    else
        version_raw_downloads=-1
    fi

    if [ "$package_type" = "container" ]; then
        # https://unix.stackexchange.com/q/550463, https://stackoverflow.com/q/45186440
        local manifest
        local manifest_ref
        local inspected_manifest
        manifest=$(awk -v RS='</pre>' '/<code.*?>/{gsub(/.*<code.*?>/, ""); print}' <<<"$version_html" | sed 's/&quot;/"/g')
        version_size=$(docker_manifest_size "$manifest")
        [[ -n "$version_tags" ]] || version_tags=$(jq '.. | try ."org.opencontainers.image.version" | select(. != null and . != "")' <<<"$manifest")

        if [[ ! "$version_size" =~ ^[0-9]+$ ]]; then
            manifest_ref="ghcr.io/$lower_owner/$lower_package$([[ "$version_name" =~ ^sha256:.+$ ]] && echo "@" || echo ":")$version_name"
            inspected_manifest=$(docker_manifest_inspect "$manifest_ref")
            (($? != 3)) || return 3
            version_size=$(docker_manifest_size "$inspected_manifest")
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
