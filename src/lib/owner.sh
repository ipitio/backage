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
    until ln "$BKG_OWNERS" "$BKG_OWNERS.lock" 2>/dev/null; do :; done
    grep -q "^(.*\/)*$owner$" "$BKG_OWNERS" || echo "$id/$owner" >>"$BKG_OWNERS"

    if [ "$(stat -c %s "$BKG_OWNERS")" -ge 100000000 ]; then
        sed -i '$d' "$BKG_OWNERS"
        return_code=2
    elif $paging && [ -n "$id" ]; then
        echo "Requested $owner"
        local last_id
        last_id=$(get_BKG BKG_LAST_SCANNED_ID)
        (( id <= last_id )) || set_BKG BKG_LAST_SCANNED_ID "$id"
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
    [ -n "$1" ] || return
    local owners_more="[]"
    local users_more="[]"
    local orgs_more="[]"

    if [ -n "$GITHUB_TOKEN" ]; then
        echo "Checking owners page $1..."
        local last_id
        last_id=$(get_BKG BKG_LAST_SCANNED_ID)
        users_more=$(query_api "users?per_page=100&page=$1&since=$last_id")
        orgs_more=$(query_api "organizations?per_page=100&page=$1&since=$last_id")
        owners_more=$(jq --argjson users "$users_more" --argjson orgs "$orgs_more" -n '$users + $orgs | unique_by(.login)')
    fi

    # if owners doesn't have .login, break
    jq -e '.[].login' <<<"$owners_more" &>/dev/null || return 2
    local owners_lines
    owners_lines=$(jq -r '.[] | @base64' <<<"$owners_more")
    run_parallel request_owner "$owners_lines"
    echo "Checked owners page $1"
    [ "$(wc -l <<<"$owners_lines")" -gt 1 ] || return 2
}

update_owner() {
    check_limit || return $?
    [ -n "$1" ] || return
    owner_id=$(cut -d'/' -f1 <<<"$1")
    owner=$(cut -d'/' -f2 <<<"$1")

    if grep -q "^$owner$" "$BKG_OPTOUT"; then
        echo "$owner was opted out!"
        rm -rf "$BKG_INDEX_DIR/${owner:?}"
        sqlite3 "$BKG_INDEX_DB" "delete from '$BKG_INDEX_TBL_PKG' where owner_id='$owner_id';"
        sqlite3 "$BKG_INDEX_DB" "drop table if exists '$(sqlite3 "$BKG_INDEX_DB" "select name from sqlite_master where type='table' and name like '${BKG_INDEX_TBL_VER}_%_${owner}_%';")';"
        set_BKG BKG_PAGE_"$owner_id" ""
        del_BKG BKG_PAGE_"$owner_id"
        return
    fi

    echo "Updating $owner..."

    # decode percent-encoded characters and make lowercase (eg. for docker manifest)
    # shellcheck disable=SC2034
    lower_owner=$(perl -pe 's/%([0-9A-Fa-f]{2})/chr(hex($1))/eg' <<<"${owner//%/%25}" | tr '[:upper:]' '[:lower:]')
    (($? != 3)) || return 3
    [ -n "$(grep -zoP 'href="/orgs/'"$owner"'/people"' <<<"$(curl "https://github.com/orgs/$owner/people")" | tr -d '\0')" ] && export owner_type="orgs" || export owner_type="users"
    [ -d "$BKG_INDEX_DIR/$owner" ] || mkdir "$BKG_INDEX_DIR/$owner"
    set_BKG BKG_PACKAGES_"$owner" ""
    #run_parallel save_package "$(sqlite3 "$BKG_INDEX_DB" "select package_type, package from '$BKG_INDEX_TBL_PKG' where owner_id = '$owner_id';" | awk -F'|' '{print "////"$1"//"$2}' | sort -uR)"
    #(($? != 3)) || return 3
    local start_page
    start_page=$(get_BKG BKG_PAGE_"$owner_id")
    [ -n "$start_page" ] || start_page=1

    for page in $(seq "$start_page" 100000); do
        local pages_left=0
        ((page <= start_page + 1)) || set_BKG BKG_PAGE_"$owner_id" "$page"
        ((page - start_page < 51)) || break
        page_package "$page"
        pages_left=$?
        run_parallel update_package "$(get_BKG_set BKG_PACKAGES_"$owner")"
        (($? != 3)) || return 3

        if ((pages_left == 2)); then
            set_BKG BKG_PAGE_"$owner_id" ""
            del_BKG BKG_PAGE_"$owner_id"
            break
        fi

        set_BKG BKG_PACKAGES_"$owner" ""
    done

    local owner_repos
    owner_repos=$(find "$BKG_INDEX_DIR/$owner" -mindepth 1 -maxdepth 1 -type d -print0 | xargs -0 -I {} basename {})

    if [ -n "$owner_repos" ]; then
        echo "Creating $owner array..."
        find "$BKG_INDEX_DIR/$owner" -type f -name '*.json' ! -name '.*' -print0 | xargs -0 jq -cs '[.] | add' >"$BKG_INDEX_DIR/$owner/.json.tmp"
        jq -cs '{ ("package"): . }' "$BKG_INDEX_DIR/$owner/.json.tmp" >"$BKG_INDEX_DIR/$owner/.json"
        ytox "$BKG_INDEX_DIR/$owner/.json"
        mv -f "$BKG_INDEX_DIR/$owner/.json.tmp" "$BKG_INDEX_DIR/$owner/.json"

        echo "Creating $owner repo arrays..."
        parallel "jq -c --arg repo {} '[.[] | select(.repo == \$repo)]' \"$BKG_INDEX_DIR/$owner/.json\" > \"$BKG_INDEX_DIR/$owner/{}/.json.tmp\"" <<<"$owner_repos"
        xargs -I {} bash -c "jq -cs '{ (\"package\"): . }' \"$BKG_INDEX_DIR/$owner/{}/.json.tmp\" > \"$BKG_INDEX_DIR/$owner/{}/.json\"" <<<"$owner_repos"
        xargs -I {} bash -c "ytox \"$BKG_INDEX_DIR/$owner/{}/.json\"" <<<"$owner_repos"
        xargs -I {} mv -f "$BKG_INDEX_DIR/$owner/{}/.json.tmp" "$BKG_INDEX_DIR/$owner/{}/.json" <<<"$owner_repos"
    fi

    sed -i '/^\(.*\/\)*'"$owner"'$/d' "$BKG_OWNERS"
    echo "Updated $owner"
}
