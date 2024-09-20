#!/bin/bash
# shellcheck disable=SC1091,SC2015

source lib/package.sh

request_owner() {
    check_limit || return $?
    [ -n "$1" ] || return
    local owner
    local id
    local return_code=0
    local paging=${2:-1}
    owner=$(_jq "$1" '.login')
    id=$(_jq "$1" '.id')
    ! grep -q "$owner" packages_all || return 1
    while ! ln "$BKG_OWNERS" "$BKG_OWNERS.lock" 2>/dev/null; do :; done
    grep -q "^.*\/*$owner$" "$BKG_OWNERS" || echo "$id/$owner" >>"$BKG_OWNERS"

    if [ "$(stat -c %s "$BKG_OWNERS")" -ge 100000000 ]; then
        sed -i '$d' "$BKG_OWNERS"
        return_code=2
    elif ((paging == 1)); then
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

    if [[ "$owner" =~ .*\/.* ]]; then
        owner_id=$(cut -d'/' -f1 <<<"$owner")
        owner=$(cut -d'/' -f2 <<<"$owner")
    fi

    if [[ ! "$owner_id" =~ ^[1-9] ]]; then
        owner_id=$(curl "https://github.com/$owner" | grep -zoP 'meta.*?u\/\d+' | tr -d '\0' | grep -oP 'u\/\d+' | sort -u | head -n1 | grep -oP '\d+')

        if [[ ! "$owner_id" =~ ^[1-9] ]]; then
            owner_id=$(query_api "users/$owner")
            (($? != 3)) || return 3
            owner_id=$(jq -r '.id' <<<"$owner_id") || return 1
        fi
    fi

    ! set_BKG_set BKG_OWNERS_QUEUE "$owner_id/$owner" || echo "Queued $owner"
}

page_owner() {
    check_limit || return $?
    [ -n "$1" ] || return
    local owners_more="[]"

    if [ -n "$GITHUB_TOKEN" ]; then
        echo "Checking owners page $1..."
        owners_more=$(query_api "users?per_page=100&page=$1&since=$(get_BKG BKG_LAST_SCANNED_ID)")
        (($? != 3)) || return 3
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
    owner_id=$(cut -d'/' -f1 <<<"$1")
    # decode percent-encoded characters and make lowercase (eg. for docker manifest)
    # shellcheck disable=SC2034
    lower_owner=$(perl -pe 's/%([0-9A-Fa-f]{2})/chr(hex($1))/eg' <<<"${owner//%/%25}" | tr '[:upper:]' '[:lower:]')
    echo "Updating $owner..."
    local ppl_html
    ppl_html=$(curl "https://github.com/orgs/$owner/people")
    [ -n "$(grep -zoP 'href="/orgs/'"$owner"'/people"' <<<"$ppl_html" | tr -d '\0')" ] && export owner_type="orgs" || export owner_type="users"

    if [ "$owner_type" = "users" ]; then
        run_parallel save_owner "$(comm -13 <(curl_orgs "$owner") <(awk -F'|' '{print $2}' <packages_all | sort -u))"
        (($? != 3)) || return 3
    else
        local users_page=1
        while :; do
            local org_members
            echo "checking org members for $owner ($users_page)..."
            org_members=$(curl_users "$owner/people?page=$users_page")
            echo "org members for $owner ($users_page): $org_members"
            run_parallel save_owner "$(comm -13 <(echo "$org_members") <(awk -F'|' '{print $2}' <packages_all | sort -u))"
            (($? != 3)) || return 3
            [ "$(wc -l <<<"$org_members")" -ge 15 ] || break
            ((users_page++))
        done
    fi

    [ -d "$BKG_INDEX_DIR/$owner" ] || mkdir "$BKG_INDEX_DIR/$owner"
    set_BKG BKG_PACKAGES_"$owner" ""
    run_parallel save_package "$(sqlite3 "$BKG_INDEX_DB" "select package_type, package from '$BKG_INDEX_TBL_PKG' where owner_id = '$owner_id';" | awk -F'|' '{print "////"$1"//"$2}' | sort -uR)"

    for page in $(seq 1 100); do
        local pages_left=0
        local pkgs
        page_package "$page"
        pages_left=$?
        pkgs=$(get_BKG_set BKG_PACKAGES_"$owner")

        if [ -z "$pkgs" ]; then
            sed -i "/^.*\/*$owner$/d" "$BKG_OWNERS"
            return 2
        fi

        ((pages_left != 3)) || return 3
        run_parallel update_package "$pkgs"
        (($? != 3)) || return 3
        ((pages_left != 2)) || break
        set_BKG BKG_PACKAGES_"$owner" ""
    done

    echo "Updated $owner"
}
