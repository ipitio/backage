#!/bin/bash
# shellcheck disable=SC1091,SC2015,SC2154

source lib/util.sh

save_package() {
    check_limit || return $?
    [ -n "$1" ] || return
    [ -n "$owner" ] || return
    local package_ref
    package_ref=$(listed_package_ref "$1") || return $?
    queue_package_ref "$package_ref"
}

listed_package_ref() {
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
    [ -n "$repo" ] || repo=$package_new
    [ -n "$repo" ] || return
    printf '%s/%s/%s\n' "$package_type" "$repo" "$package_new"
}

queue_package_ref() {
    [ -n "$1" ] || return
    [ -n "$owner" ] || return
    local package_new
    local package_type
    local repo

    package_type=$(cut -d'/' -f1 <<<"$1")
    repo=$(cut -d'/' -f2 <<<"$1")
    package_new=$(cut -d'/' -f3- <<<"$1")
    [ -n "$package_type" ] || return 1
    [ -n "$repo" ] || return 1
    [ -n "$package_new" ] || return 1
    ! awk -F'|' -v owner_id_key="$owner_id" -v owner_key="$owner" -v repo_key="$repo" -v package_key="$package_new" '$1 == owner_id_key && $2 == owner_key && $3 == repo_key && $4 == package_key { found = 1; exit } END { exit !found }' packages_already_updated || return
    ! set_BKG_set BKG_PACKAGES_"$owner" "$package_type/$repo/$package_new" || echo "Queued $owner/$package_new"
}

page_package() {
    [ -n "$1" ] || return
    [ -n "$owner" ] || return
    local package_href
    local package_ref
    local package_refs_file
    local packages_lines
    local status=0
    echo "Starting $owner page $1..."
    if [ "$owner_type" = "users" ]; then
        pkg_html=$(curl "https://github.com/$owner?tab=packages$([ "$BKG_MODE" -lt 2 ] && echo "&visibility=public" || { [ "$BKG_MODE" -eq 5 ] && echo "&visibility=private" || echo ""; })&per_page=100&page=$1")
    else
        pkg_html=$(curl "https://github.com/$owner_type/$owner/packages?per_page=100$([ "$BKG_MODE" -lt 2 ] && echo "&visibility=public" || { [ "$BKG_MODE" -eq 5 ] && echo "&visibility=private" || echo ""; })&page=$1")
    fi
    status=$?
    ((status != 3)) || return 3
    ((status == 0)) || return 1
    packages_lines=$(grep -zoP 'href="/'"$owner_type"'/'"$owner"'/packages/[^/]+/package/[^"]+"' <<<"$pkg_html" | tr -d '\0')
    [ -n "$packages_lines" ] || return 2
    packages_lines=${packages_lines//href=/\\nhref=}
    packages_lines=${packages_lines//\\n/$'\n'}
    package_refs_file=$(mktemp) || return 1
    while IFS= read -r package_href; do
        [ -n "$package_href" ] || continue
        package_ref=$(listed_package_ref "$package_href") || {
            rm -f "$package_refs_file"
            return 1
        }
        printf '%s\n' "$package_ref" >>"$package_refs_file"
    done <<<"$packages_lines"
    sort -u "$package_refs_file" -o "$package_refs_file"
    if [ -n "${OWNER_SCAN_MARKER:-}" ]; then
        owner_scan_observe_file "$package_refs_file" || {
            status=$?
            rm -f "$package_refs_file"
            return "$status"
        }
    fi
    run_parallel queue_package_ref "$(cat "$package_refs_file")"
    status=$?
    rm -f "$package_refs_file"
    ((status != 3)) || return 3
    (($1 > 1)) || grep -q href <<<"$packages_lines" || sed -i '/^\(.*\/\)*'"$owner"'$/d' "$BKG_OWNERS"
    echo "Started $owner page $1"
    [ "$(wc -l <<<"$packages_lines")" -gt 1 ] || return 2
}

package_render_json_from_db_context() {
    [ -n "$1" ] || return
    local output_file=$1
    local render_since_date=${2:-}
    local version_limit=${3:--1}
    local output_date=${4:-$render_since_date}

    [ -n "$render_since_date" ] || render_since_date=$(current_batch_first_started)
    [ -n "$render_since_date" ] || render_since_date="0000-00-00"
    [ -n "$output_date" ] || output_date="-"
    [[ "$version_limit" =~ ^-?[0-9]+$ ]] || version_limit=-1
    bkg_python render package \
        "$owner_id" "$owner_type" "$package_type" "$owner" "$repo" "$package" \
        "$table_version_name" "$render_since_date" "$output_date" \
        "$version_limit" "$output_file"
}

update_package() {
    check_limit || return $?
    [ -n "$1" ] || return
    [ -n "$owner" ] || return
    local package_ref=${1%/}
    local remainder
    local write_legacy=true
    local use_rest_api=false
    local refresh_summary=""
    local refresh_status=0
    local outcome=""
    local batch_first_started=""

    package_type=${package_ref%%/*}
    remainder=${package_ref#*/}
    repo=${remainder%%/*}
    package=${remainder#*/}
    [ -n "$package_type" ] || return 1
    [ -n "$repo" ] || return 1
    [ -n "$package" ] || return 1
    table_version_name="${BKG_INDEX_TBL_VER}_${owner_type}_${package_type}_${owner}_${repo}_${package}"
    [ -z "${GITHUB_TOKEN:-}" ] || use_rest_api=true
    batch_first_started=$(current_batch_first_started)
    [ -n "$batch_first_started" ] || batch_first_started="0000-00-00"

    echo "Updating $owner/$package..."
    refresh_summary=$(bkg_python package refresh \
        "$owner_id" "$owner_type" "$package_type" "$owner" "$repo" "$package" \
        "$table_version_name" "$batch_first_started" \
        "$write_legacy" "$use_rest_api" "${fast_out:-false}") || refresh_status=$?

    ((refresh_status != 3)) || return 3
    if ((refresh_status != 0)); then
        echo "Package refresh failed for $owner/$package" >&2
        return "$refresh_status"
    fi
    [ -n "$refresh_summary" ] || return 0
    echo "Package refresh summary for $owner/$package: $refresh_summary"
    outcome=$(jq -r '.outcome // empty' <<<"$refresh_summary" 2>/dev/null)
    case "$outcome" in
    opted_out)
        echo "$owner/$package was opted out!"
        ;;
    fast_out) ;;
    metadata_unavailable)
        echo "Package metadata unavailable for $owner/$package; leaving it pending" >&2
        ;;
    *)
        echo "Refreshed $owner/$package"
        ;;
    esac
}
