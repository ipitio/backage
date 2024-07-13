#!/bin/bash
# Scrape each package
# Usage: ./update.sh
# Dependencies: curl, jq, sqlite3, docker
# Copyright (c) ipitio
#
# shellcheck disable=SC1091,SC2015

cd "${0%/*}" || exit
source lib.sh

main() {
    # remove owners from owners.txt that have already been scraped in this batch
    [ -n "$(get_BKG BKG_BATCH_FIRST_STARTED)" ] || set_BKG BKG_BATCH_FIRST_STARTED "$TODAY"

    if [ -s "$(get_BKG BKG_OWNERS)" ] && [ "$1" = "0" ]; then
        owners_to_remove=()

        while IFS= read -r owner; do
            check_limit || return
            [ -n "$owner" ] || continue
            [[ "$owner" =~ .*\/.* ]] && owner_id=$(cut -d'/' -f1 <<<"$owner") || owner_id=""

            if [ -z "$owner_id" ]; then
                query="select count(*) from '$(get_BKG BKG_INDEX_TBL_PKG)' where owner='$owner' and date between date('$(get_BKG BKG_BATCH_FIRST_STARTED)') and date('$TODAY');"
            else
                query="select count(*) from '$(get_BKG BKG_INDEX_TBL_PKG)' where owner_id='$owner_id' and date between date('$(get_BKG BKG_BATCH_FIRST_STARTED)') and date('$TODAY');"
            fi

            count=$(sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query")
            [[ "$count" =~ ^0*$ ]] || owners_to_remove+=("$owner")
        done <"$(get_BKG BKG_OWNERS)"

        for owner_to_remove in "${owners_to_remove[@]}"; do
            sed -i "/$owner_to_remove/d" "$(get_BKG BKG_OWNERS)"
        done
    fi

    [ -s "$(get_BKG BKG_OWNERS)" ] || set_BKG BKG_BATCH_FIRST_STARTED "$TODAY"
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

    # if this is a scheduled update, scrape all owners that haven't been scraped in this batch
    if [ "$1" = "0" ]; then
        # get more owners if no more
        if [ ! -s "$(get_BKG BKG_OWNERS)" ]; then
            # get new owners
            echo "Finding more owners..."
            owners_page=0
            [ -n "$(get_BKG BKG_LAST_SCANNED_ID)" ] || set_BKG BKG_LAST_SCANNED_ID "0"
            last_scanned_id=$(get_BKG BKG_LAST_SCANNED_ID)
            while [ "$owners_page" -lt 10 ]; do
                check_limit || return
                ((owners_page++))
                owners_more="[]"

                if [ -n "$GITHUB_TOKEN" ]; then
                    owners_more=$(curl -H "Accept: application/vnd.github+json" \
                        -H "Authorization: Bearer $GITHUB_TOKEN" \
                        -H "X-GitHub-Api-Version: 2022-11-28" \
                        "https://api.github.com/users?per_page=100&page=$owners_page&since=$last_scanned_id")
                    calls_to_api=$(get_BKG BKG_CALLS_TO_API)
                    min_calls_to_api=$(get_BKG BKG_MIN_CALLS_TO_API)
                    ((calls_to_api++))
                    ((min_calls_to_api++))
                    set_BKG BKG_CALLS_TO_API "$calls_to_api"
                    set_BKG BKG_MIN_CALLS_TO_API "$min_calls_to_api"
                    jq -e . <<<"$owners_more" &>/dev/null || owners_more="[]"
                fi

                # if owners doesn't have .login, break
                jq -e '.[].login' <<<"$owners_more" &>/dev/null || break

                # add the new owners to the owners array
                for i in $(jq -r '.[] | @base64' <<<"$owners_more"); do
                    _jq() {
                        echo "$i" | base64 --decode | jq -r "$@"
                    }

                    owner=$(_jq '.login')
                    id=$(_jq '.id')
                    [ -n "$owner" ] || continue
                    grep -q "$owner" "$(get_BKG BKG_OWNERS)" || echo "$id/$owner" >>"$(get_BKG BKG_OWNERS)"
                done
            done
        fi

        # add the owners in the database to the owners array
        echo "Reading known owners..."
        query="select owner_id, owner from '$(get_BKG BKG_INDEX_TBL_PKG)' where date not between date('$(get_BKG BKG_BATCH_FIRST_STARTED)') and date('$TODAY') group by owner_id;"

        while IFS= read -r owner_id owner; do
            check_limit || return
            [ -n "$owner" ] || continue
            grep -q "$owner" "$(get_BKG BKG_OWNERS)" || echo "$owner_id/$owner" >>"$(get_BKG BKG_OWNERS)"
        done < <(sqlite3 "$(get_BKG BKG_INDEX_DB)" "$query")
    fi

    owners=()

    # add more owners
    if [ -s "$(get_BKG BKG_OWNERS)" ]; then
        echo "Queuing owners..."
        sed -i '/^\s*$/d' "$(get_BKG BKG_OWNERS)"
        echo >>"$(get_BKG BKG_OWNERS)"
        awk 'NF' "$(get_BKG BKG_OWNERS)" >owners.tmp && mv owners.tmp "$(get_BKG BKG_OWNERS)"
        sed -i 's/^[[:space:]]*//;s/[[:space:]]*$//' "$(get_BKG BKG_OWNERS)"

        while IFS= read -r owner; do
            check_limit || return
            owner=$(echo "$owner" | tr -d '[:space:]')
            [ -n "$owner" ] || continue
            owner_id=""

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

            grep -q "$owner_id/$owner" <<<"${owners[*]}" || owners+=("$owner_id/$owner")
        done <"$(get_BKG BKG_OWNERS)"
    fi

    # scrape the owners
    echo "Forking jobs..."
    printf "%s\n" "${owners[@]}" | env_parallel -j 100% --lb update_owner
    echo "Completed jobs"
    xz_db
    return $?
}

main "$@"
exit $?
