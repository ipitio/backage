#!/bin/bash
# Refresh json and README
# Usage: ./refre.sh
# Dependencies: jq, sqlite3
# Copyright (c) ipitio
#
# shellcheck disable=SC1091,SC2015

cd "${0%/*}" || exit
source lib.sh

main() {
    [ ! -f ../README.md ] || rm -f ../README.md
    \cp ../templates/.README.md ../README.md
    perl -0777 -pe 's/<GITHUB_OWNER>/'"$GITHUB_OWNER"'/g; s/<GITHUB_REPO>/'"$GITHUB_REPO"'/g; s/<GITHUB_BRANCH>/'"$GITHUB_BRANCH"'/g' ../README.md >README.tmp && [ -f README.tmp ] && mv README.tmp ../README.md || :
    owners=$(sqlite3 "$BKG_INDEX_DB" "select distinct owner from '$BKG_INDEX_TBL_PKG';")
    echo "$owners" | env_parallel --lb refresh_owner

    for owner in $owners; do
        if [ ! -f "$BKG_INDEX_DIR"/"$owner".json ] || jq -e 'length == 0' "$BKG_INDEX_DIR"/"$owner".json; then
            return 1
        fi
    done
}

main "$@"
exit $?
