#!/bin/bash
# shellcheck disable=SC1091,SC2015

source lib/package.sh

request_owner() {
    [ -n "$1" ] || return
    local owner=""
    local id=""
    local return_code=0
    local paging=true
    owner=$(_jq "$1" '.login' 2>/dev/null)
    [ -n "$owner" ] && id=$(_jq "$1" '.id' 2>/dev/null) || paging=false

    if [ -z "$id" ]; then
        owner=$(owner_get_id "$1")
        id=$(cut -d'/' -f1 <<<"$owner")
        owner=$(cut -d'/' -f2 <<<"$owner")
    fi

    ! grep -q "$owner" packages_all || return 1
    while ! ln "$BKG_OWNERS" "$BKG_OWNERS.lock" 2>/dev/null; do :; done
    grep -q "^(.*\/)*$owner$" "$BKG_OWNERS" || echo "$id/$owner" >>"$BKG_OWNERS"

    if [ "$(stat -c %s "$BKG_OWNERS")" -ge 100000000 ]; then
        sed -i '$d' "$BKG_OWNERS"
        return_code=2
    elif $paging && [ -n "$id" ]; then
        set_BKG BKG_LAST_SCANNED_ID "$id"
    fi

    rm -f "$BKG_OWNERS.lock"
    return $return_code
}

save_owner() {
    [ -n "$1" ] || return
    local owner_id
    owner_id=$(owner_get_id "$1") || return
    ! set_BKG_set BKG_OWNERS_QUEUE "$owner_id" || echo "Queued $(cut -d'/' -f2 <<<"$owner_id")"
}

page_owner() {
    ((${1:-0} > 0)) || return
    local owners_more="[]"
    local users_more="[]"
    local orgs_more="[]"

    if [ -n "$GITHUB_TOKEN" ]; then
        echo "Checking owners page $1..."
        users_more=$(query_api "users?per_page=100&page=$1&since=$(get_BKG BKG_LAST_SCANNED_ID)")
        orgs_more=$(query_api "organizations?per_page=100&page=$1&since=$(get_BKG BKG_LAST_SCANNED_ID)")
        owners_more=$(jq --argjson users "$users_more" --argjson orgs "$orgs_more" -n '$users + $orgs | unique_by(.login))')
        (($? != 3)) || return 3
    fi

    # if owners doesn't have .login, break
    jq -e '.[].login' <<<"$owners_more" &>/dev/null || return 2
    local owners_lines
    owners_lines=$(jq -r '.[] | @base64' <<<"$owners_more")
    run_parallel request_owner "$owners_lines"
    (($? != 3)) || return 3
    echo "Checked owners page $1"
    [ "$(wc -l <<<"$owners_lines")" -gt 0 ] || return 2
}

update_owner() {
    check_limit || return $?
    [ -n "$1" ] || return
    local ppl_html
    owner=$(cut -d'/' -f2 <<<"$1")
    owner_id=$(cut -d'/' -f1 <<<"$1")
    echo "Updating $owner..."

    # decode percent-encoded characters and make lowercase (eg. for docker manifest)
    # shellcheck disable=SC2034
    lower_owner=$(perl -pe 's/%([0-9A-Fa-f]{2})/chr(hex($1))/eg' <<<"${owner//%/%25}" | tr '[:upper:]' '[:lower:]')
    ppl_html=$(curl "https://github.com/orgs/$owner/people")
    (($? != 3)) || return 3
    [ -n "$(grep -zoP 'href="/orgs/'"$owner"'/people"' <<<"$ppl_html" | tr -d '\0')" ] && export owner_type="orgs" || export owner_type="users"
    [ -d "$BKG_INDEX_DIR/$owner" ] || mkdir "$BKG_INDEX_DIR/$owner"
    set_BKG BKG_PACKAGES_"$owner" ""
    run_parallel save_package "$(sqlite3 "$BKG_INDEX_DB" "select package_type, package from '$BKG_INDEX_TBL_PKG' where owner_id = '$owner_id';" | awk -F'|' '{print "////"$1"//"$2}' | sort -uR)"
    (($? != 3)) || return 3

    for page in $(seq 1 100); do
        local pages_left=0
        local pkgs
        page_package "$page"
        pages_left=$?
        pkgs=$(get_BKG_set BKG_PACKAGES_"$owner")
        run_parallel update_package "$pkgs"
        (($? != 3)) || return 3
        ((pages_left != 2)) || break
        set_BKG BKG_PACKAGES_"$owner" ""
    done

    sed -i '/^\(.*\/\)*'"$owner"'$/d' "$BKG_OWNERS"
    echo "Updated $owner"
}
