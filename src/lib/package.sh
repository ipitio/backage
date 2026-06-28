#!/bin/bash
# shellcheck disable=SC1091,SC2015,SC2034,SC2154

source lib/util.sh

PACKAGE_PAGE_WORK=""
PACKAGE_PAGE_OWNER_MISSING=false
PACKAGE_PAGE_LISTING_UNAVAILABLE=false

page_package() {
    [ -n "$1" ] || return
    [ -n "$owner" ] || return
    local has_more
    local listing
    local batch_first_started
    local package_count
    local owner_missing
    local listing_unavailable
    local status=0
    PACKAGE_PAGE_WORK=""
    PACKAGE_PAGE_OWNER_MISSING=false
    PACKAGE_PAGE_LISTING_UNAVAILABLE=false
    batch_first_started=$(current_batch_first_started)
    [ -n "$batch_first_started" ] || batch_first_started="0000-00-00"
    echo "Starting $owner page $1..."
    listing=$(bkg_python package list-page \
        "$owner_id" "$owner_type" "$owner" "$1" \
        "${OWNER_SCAN_MARKER:--}" "$batch_first_started" \
        "$(date -u +%s)") || status=$?
    ((status != 3)) || return 3
    ((status == 0)) || return 1
    PACKAGE_PAGE_WORK=$(jq -r '.packages[] | [.package_type, .repo, .package] | join("/")' <<<"$listing") || return 1
    package_count=$(jq -r '.observed_count' <<<"$listing") || return 1
    has_more=$(jq -r '.has_more' <<<"$listing") || return 1
    owner_missing=$(jq -r '.owner_missing' <<<"$listing") || return 1
    listing_unavailable=$(jq -r '.listing_unavailable' <<<"$listing") || return 1
    [ "$owner_missing" = true ] && PACKAGE_PAGE_OWNER_MISSING=true
    [ "$listing_unavailable" = true ] && PACKAGE_PAGE_LISTING_UNAVAILABLE=true
    (($1 > 1 || package_count > 0)) || sed -i '/^\(.*\/\)*'"$owner"'$/d' "$BKG_OWNERS"
    echo "Started $owner page $1"
    if $PACKAGE_PAGE_LISTING_UNAVAILABLE; then
        echo "Package listing unavailable for existing owner $owner; verifying known packages individually"
    fi
    [ "$has_more" = true ] || return 2
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
