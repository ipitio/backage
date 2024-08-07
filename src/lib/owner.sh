#!/bin/bash
# shellcheck disable=SC1091,SC2015

source lib/package.sh

request_owner() {
    check_limit || return $?
    [ -n "$1" ] || return
    local owner
    local id
    owner=$(_jq "$1" '.login')
    id=$(_jq "$1" '.id')

    while ! ln "$BKG_OWNERS" "$BKG_OWNERS.lock" 2>/dev/null; do
        sleep 0.1
    done

    grep -q "^.*\/*$owner$" "$BKG_OWNERS" || echo "$id/$owner" >>"$BKG_OWNERS"
    local return_code=0

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

    set_BKG_set BKG_OWNERS_QUEUE "$owner_id/$owner"
    echo "Queued $owner"
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
        jq -e . <<<"$owners_more" &>/dev/null || owners_more="[]"
    fi

    # if owners doesn't have .login, break
    jq -e '.[].login' <<<"$owners_more" &>/dev/null || return 2
    local owners_lines
    owners_lines=$(jq -r '.[] | @base64' <<<"$owners_more")
    run_parallel request_owner "$owners_lines" || return $?
    echo "Checked owners page $1"
    # if there are fewer than 100 lines, break
    [ "$(wc -l <<<"$owners_lines")" -eq 100 ] || return 2
}

update_owner() {
    check_limit || return $?
    [ -n "$1" ] || return
    owner=$(cut -d'/' -f2 <<<"$1")
    owner_id=$(cut -d'/' -f1 <<<"$1")
    echo "Updating $owner..."
    [ -n "$(curl "https://github.com/orgs/$owner/people" | grep -zoP 'href="/orgs/'"$owner"'/people"' | tr -d '\0')" ] && export owner_type="orgs" || export owner_type="users"

    for page in $(seq 1 100); do
        local pages_left=0
        set_BKG BKG_PACKAGES_"$owner" ""
        page_package "$page"
        pages_left=$?
        ((pages_left != 3)) || return 3
        run_parallel update_package "$(get_BKG_set BKG_PACKAGES_"$owner")" || return $?
        del_BKG BKG_PACKAGES_"$owner"
        ((pages_left != 2)) || break
    done

    echo "Updated $owner"
}

refresh_owner() {
    check_limit 21500 || return $?
    [ -n "$1" ] || return
    echo "Refreshing $1..."
    [ -d "$BKG_INDEX_DIR" ] || mkdir "$BKG_INDEX_DIR"
    [ -d "$BKG_INDEX_DIR/$1" ] || mkdir "$BKG_INDEX_DIR/$1"
    run_parallel refresh_package "$(sqlite3 "$BKG_INDEX_DB" "select * from '$BKG_INDEX_TBL_PKG' where owner='$1' and date >= '$BKG_BATCH_FIRST_STARTED' group by package;")"
    echo "Refreshed $1"
}

update_owners() {
    [ -n "$(get_BKG BKG_LAST_SCANNED_ID)" ] || set_BKG BKG_LAST_SCANNED_ID "0"
    local rotated=false
    local owners
    local repos
    local packages
    local mode
    mode=$(get_BKG BKG_AUTO)
    sqlite3 "$BKG_INDEX_DB" "select owner_id, owner, repo, package from '$BKG_INDEX_TBL_PKG' where date >= '$BKG_BATCH_FIRST_STARTED' group by owner_id, owner, repo, package;" | sort -u >packages_already_updated
    sqlite3 "$BKG_INDEX_DB" "select owner_id, owner, repo, package from '$BKG_INDEX_TBL_PKG' group by owner_id, owner, repo, package;" | sort -u >packages_all
    set_BKG BKG_OWNERS_QUEUE ""

    # if this is a scheduled update, scrape all owners
    if [ "$mode" -eq 0 ]; then
        comm -13 packages_already_updated packages_all >packages_to_update
        echo "all: $(wc -l <packages_all)"
        echo "done: $(wc -l <packages_already_updated)"
        echo "left: $(wc -l <packages_to_update)"
        awk -F'|' '{print $1"/"$2}' <packages_to_update | sort -u | env_parallel --lb save_owner

        if [ -z "$(get_BKG_set BKG_OWNERS_QUEUE)" ]; then
            set_BKG BKG_BATCH_FIRST_STARTED "$BKG_TODAY"
            [ -s "$BKG_OWNERS" ] || seq 1 10 | env_parallel --lb --halt soon,fail=1 page_owner
            awk -F'|' '{print $1"/"$2}' <packages_all | sort -u | env_parallel --lb save_owner
        else
            [ -n "$(get_BKG BKG_BATCH_FIRST_STARTED)" ] || set_BKG BKG_BATCH_FIRST_STARTED "$BKG_TODAY"
        fi
    elif [ "$mode" -eq 1 ]; then
        save_owner arevindh
    fi

    # add more owners
    if [ -s "$BKG_OWNERS" ]; then
        sed -i '/^\s*$/d' "$BKG_OWNERS"
        echo >>"$BKG_OWNERS"
        awk 'NF' "$BKG_OWNERS" >owners.tmp && mv owners.tmp "$BKG_OWNERS"
        sed -i 's/^[[:space:]]*//;s/[[:space:]]*$//' "$BKG_OWNERS"
        awk '!seen[$0]++' "$BKG_OWNERS" >owners.tmp && mv owners.tmp "$BKG_OWNERS"
        # remove lines from $BKG_OWNERS that are in $packages_all
        echo "$(
            awk -F'|' '{print $1"/"$2}' <packages_all
            awk -F'|' '{print $2}' <packages_all
        )" | sort -u | parallel "sed -i '\,^{}$,d' $BKG_OWNERS"
        env_parallel --lb save_owner <"$BKG_OWNERS"
    fi

    BKG_BATCH_FIRST_STARTED=$(get_BKG BKG_BATCH_FIRST_STARTED)
    [ -n "$(get_BKG BKG_RATE_LIMIT_START)" ] || set_BKG BKG_RATE_LIMIT_START "$(date -u +%s)"
    [ -n "$(get_BKG BKG_CALLS_TO_API)" ] || set_BKG BKG_CALLS_TO_API "0"

    # reset the rate limit if an hour has passed since the last run started
    if (($(get_BKG BKG_RATE_LIMIT_START) + 3600 <= $(date -u +%s))); then
        set_BKG BKG_RATE_LIMIT_START "$(date -u +%s)"
        set_BKG BKG_CALLS_TO_API "0"
    fi

    # reset the secondary rate limit if a minute has passed since the last run started
    if (($(get_BKG BKG_MIN_RATE_LIMIT_START) + 60 <= $(date -u +%s))); then
        set_BKG BKG_MIN_RATE_LIMIT_START "$(date -u +%s)"
        set_BKG BKG_MIN_CALLS_TO_API "0"
    fi

    get_BKG_set BKG_OWNERS_QUEUE | env_parallel --lb update_owner
    echo "Compressing the database..."
    sqlite3 "$BKG_INDEX_DB" ".dump" | zstd -22 --ultra --long -T0 -o "$BKG_INDEX_SQL".new.zst

    if [ -f "$BKG_INDEX_SQL".new.zst ]; then
        # rotate the database if it's greater than 2GB
        if [ -f "$BKG_INDEX_SQL".zst ] && [ "$(stat -c %s "$BKG_INDEX_SQL".new.zst)" -ge 2000000000 ]; then
            rotated=true
            echo "Rotating the database..."
            local older_db
            older_db="$(date -u +%Y.%m.%d)".zst
            [ ! -f "$older_db" ] || rm -f "$older_db"
            mv "$BKG_INDEX_SQL".zst "$older_db"
            sqlite3 "$BKG_INDEX_DB" "delete from '$BKG_INDEX_TBL_PKG' where date < '$BKG_BATCH_FIRST_STARTED';"
            sqlite3 "$BKG_INDEX_DB" "select name from sqlite_master where type='table' and name like '${BKG_INDEX_TBL_VER}_%';" | parallel --lb "sqlite3 '$BKG_INDEX_DB' 'delete from {} where date < \"$BKG_BATCH_FIRST_STARTED\";'"
            sqlite3 "$BKG_INDEX_DB" "vacuum;"
            rm -f "$BKG_INDEX_SQL".new.zst
            sqlite3 "$BKG_INDEX_DB" ".dump" | zstd -22 --ultra --long -T0 -o "$BKG_INDEX_SQL".new.zst
            echo "Rotated the database"
        fi

        mv "$BKG_INDEX_SQL".new.zst "$BKG_INDEX_SQL".zst
        echo "Compressed the database"
    else
        echo "Failed to compress the database!"
    fi

    echo "Updating templates..."
    [ ! -f "$BKG_ROOT"/CHANGELOG.md ] || rm -f "$BKG_ROOT"/CHANGELOG.md
    \cp templates/.CHANGELOG.md "$BKG_ROOT"/CHANGELOG.md
    owners=$(sqlite3 "$BKG_INDEX_DB" "select count(distinct owner_id) from '$BKG_INDEX_TBL_PKG';")
    repos=$(sqlite3 "$BKG_INDEX_DB" "select count(distinct repo) from '$BKG_INDEX_TBL_PKG';")
    packages=$(sqlite3 "$BKG_INDEX_DB" "select count(distinct package) from '$BKG_INDEX_TBL_PKG';")
    perl -0777 -pe 's/\[OWNERS\]/'"$owners"'/g; s/\[REPOS\]/'"$repos"'/g; s/\[PACKAGES\]/'"$packages"'/g' "$BKG_ROOT"/CHANGELOG.md >CHANGELOG.tmp && [ -f CHANGELOG.tmp ] && mv CHANGELOG.tmp "$BKG_ROOT"/CHANGELOG.md || :
    ! $rotated || echo " The database grew over 2GB and was rotated, but you can find all previous data under [Releases](https://github.com/$GITHUB_OWNER/$GITHUB_REPO/releases)." >>"$BKG_ROOT"/CHANGELOG.md
    [ ! -f "$BKG_ROOT"/README.md ] || rm -f "$BKG_ROOT"/README.md
    \cp templates/.README.md "$BKG_ROOT"/README.md
    perl -0777 -pe 's/<GITHUB_OWNER>/'"$GITHUB_OWNER"'/g; s/<GITHUB_REPO>/'"$GITHUB_REPO"'/g; s/<GITHUB_BRANCH>/'"$GITHUB_BRANCH"'/g' "$BKG_ROOT"/README.md >README.tmp && [ -f README.tmp ] && mv README.tmp "$BKG_ROOT"/README.md || :
    rm -f packages_already_updated packages_all packages_to_update
    echo "Updated templates"
}
