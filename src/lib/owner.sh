#!/bin/bash
# shellcheck disable=SC1091,SC2015

source lib/package.sh

request_owner() {
    check_limit || return $?
    [ -n "$1" ] || return
    local owner
    local id
    local return_code=0
    owner=$(_jq "$1" '.login')
    id=$(_jq "$1" '.id')
    while ! ln "$BKG_OWNERS" "$BKG_OWNERS.lock" 2>/dev/null; do :; done
    grep -q "^.*\/*$owner$" "$BKG_OWNERS" || echo "$id/$owner" >>"$BKG_OWNERS"

    if [ "$(stat -c %s "$BKG_OWNERS")" -ge 100000000 ]; then
        sed -i '$d' "$BKG_OWNERS"
        return_code=2
    else
        set_BKG BKG_LAST_SCANNED_ID "$id"
    fi

    rm -f "$BKG_OWNERS.lock"
    return $return_code
}

save_owner() {
    check_limit || return $?
    owner=$(echo "$1" | tr -d '[:space:]')
    [ -n "$owner" ] || return
    owner_id=""
    local calls_to_api
    local min_calls_to_api

    if [[ "$owner" =~ .*\/.* ]]; then
        owner_id=$(cut -d'/' -f1 <<<"$owner")
        owner=$(cut -d'/' -f2 <<<"$owner")
    fi

    if [ -z "$owner_id" ]; then
        owner_id=$(curl -H "Accept: application/vnd.github+json" \
            -H "Authorization: Bearer $GITHUB_TOKEN" \
            -H "X-GitHub-Api-Version: 2022-11-28" \
            "https://api.github.com/users/$owner" | jq -r '.id')
        calls_to_api=$(get_BKG BKG_CALLS_TO_API)
        min_calls_to_api=$(get_BKG BKG_MIN_CALLS_TO_API)
        ((calls_to_api++))
        ((min_calls_to_api++))
        set_BKG BKG_CALLS_TO_API "$calls_to_api"
        set_BKG BKG_MIN_CALLS_TO_API "$min_calls_to_api"
    fi

    ! set_BKG_set BKG_OWNERS_QUEUE "$owner_id/$owner" || echo "Queued $owner"
}

page_owner() {
    check_limit || return $?
    [ -n "$1" ] || return
    local owners_more="[]"
    local calls_to_api
    local min_calls_to_api

    if [ -n "$GITHUB_TOKEN" ]; then
        echo "Checking owners page $1..."
        owners_more=$(curl -H "Accept: application/vnd.github+json" \
            -H "Authorization: Bearer $GITHUB_TOKEN" \
            -H "X-GitHub-Api-Version: 2022-11-28" \
            "https://api.github.com/users?per_page=100&page=$1&since=$(get_BKG BKG_LAST_SCANNED_ID)")
        (($? != 3)) || return 3
        calls_to_api=$(get_BKG BKG_CALLS_TO_API)
        min_calls_to_api=$(get_BKG BKG_MIN_CALLS_TO_API)
        ((calls_to_api++))
        ((min_calls_to_api++))
        set_BKG BKG_CALLS_TO_API "$calls_to_api"
        set_BKG BKG_MIN_CALLS_TO_API "$min_calls_to_api"
    fi

    # if owners doesn't have .login, break
    jq -e '.[].login' <<<"$owners_more" &>/dev/null || return 2
    local owners_lines
    owners_lines=$(jq -r '.[] | @base64' <<<"$owners_more")
    run_parallel request_owner "$owners_lines"
    (($? != 3)) || return 3
    echo "Checked owners page $1"
    # if there are fewer than 100 lines, break
    [ "$(wc -l <<<"$owners_lines")" -eq 100 ] || return 2
}

update_owner() {
    check_limit || return $?
    [ -n "$1" ] || return
    owner=$(cut -d'/' -f2 <<<"$1")

    if grep -q "^$owner$" "$BKG_OPTOUT"; then
        echo "$owner was opted out!"
        rm -rf "${BKG_INDEX_DIR:?}/$owner"
        sqlite3 "$BKG_INDEX_DB" "delete from '$BKG_INDEX_TBL_PKG' where owner_id='$owner_id';"
        sqlite3 "$BKG_INDEX_DB" "select name from sqlite_master where type='table' and name like '${BKG_INDEX_TBL_VER}_${owner_type}_${package_type}_${owner}_%';" | parallel sqlite3 "$BKG_INDEX_DB" "drop table if exists '{}';"
        return
    fi

    owner_id=$(cut -d'/' -f1 <<<"$1")
    # decode percent-encoded characters and make lowercase (eg. for docker manifest)
    # shellcheck disable=SC2034
    lower_owner=$(perl -pe 's/%([0-9A-Fa-f]{2})/chr(hex($1))/eg' <<<"${owner//%/%25}" | tr '[:upper:]' '[:lower:]')
    echo "Updating $owner..."
    [ -n "$(curl "https://github.com/orgs/$owner/people" | grep -zoP 'href="/orgs/'"$owner"'/people"' | tr -d '\0')" ] && export owner_type="orgs" || export owner_type="users"
    [ -d "$BKG_INDEX_DIR/$owner" ] || mkdir "$BKG_INDEX_DIR/$owner"
    set_BKG BKG_PACKAGES_"$owner" ""
    run_parallel save_package "$(sqlite3 "$BKG_INDEX_DB" "select package_type, package from '$BKG_INDEX_TBL_PKG' where owner_id = '$owner_id';" | awk -F'|' '{print "////"$1"//"$2}' | sort -uR)"

    for page in $(seq 1 100); do
        local pages_left=0
        page_package "$page"
        pages_left=$?
        ((pages_left != 3)) || return 3
        run_parallel update_package "$(get_BKG_set BKG_PACKAGES_"$owner")"
        (($? != 3)) || return 3
        set_BKG BKG_PACKAGES_"$owner" ""
        ((pages_left != 2)) || break
    done

    echo "Updated $owner"
}
